"""Code to identify if a principal in an AWS account can use access to Lambda to access other principals."""

#  Copyright NCC Group (c) 2019. This file is part of Principal Mapper.
#
#      Principal Mapper is free software: you can redistribute it and/or modify
#      it under the terms of the GNU Affero General Public License as published by
#      the Free Software Foundation, either version 3 of the License, or
#      (at your option) any later version.
#
#      Principal Mapper is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#      GNU Affero General Public License for more details.
#
#      You should have received a copy of the GNU Affero General Public License
#      along with Principal Mapper.  If not, see <https://www.gnu.org/licenses/>.
#
#      Principal Mapper is free software: you can redistribute it and/or modify
#      it under the terms of the GNU Affero General Public License as published by
#      the Free Software Foundation, either version 3 of the License, or
#      (at your option) any later version.
#
#      Principal Mapper is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#      GNU General Public License for more details.
#
#      You should have received a copy of the GNU Affero General Public License
#      along with Foobar.  If not, see <https://www.gnu.org/licenses/>.

import io
import os
from typing import List

from botocore.exceptions import ClientError

from principalmapper.common.edges import Edge
from principalmapper.common.nodes import Node
from principalmapper.graphing.edge_checker import EdgeChecker
from principalmapper.querying.local_policy_simulation import resource_policy_authorization, ResourcePolicyEvalResult
from principalmapper.querying import query_interface
from principalmapper.util import arns


class LambdaEdgeChecker(EdgeChecker):
    """Goes through the CloudFormation service to locate potential edges between nodes."""

    def return_edges(self, nodes: List[Node], output: io.StringIO = os.devnull, debug: bool = False) -> List[Edge]:
        """Fulfills expected method return_edges. If session object is None, runs checks in offline mode."""
        result = []

        lambda_clients = []
        if self.session is not None:
            print('Searching through Lambda-supported regions for existing functions.')
            lambda_regions = self.session.get_available_regions('lambda')
            for region in lambda_regions:
                lambda_clients.append(self.session.create_client('lambda', region_name=region))

        # grab existing lambda functions
        function_list = []
        for lambda_client in lambda_clients:
            try:
                paginator = lambda_client.get_paginator('list_functions')
                for page in paginator.paginate(PaginationConfig={'PageSize': 25}):
                    for func in page['Functions']:
                        function_list.append(func)
            except ClientError:
                output.write('Encountered an exception when listing functions in the region {}\n'.format(
                    lambda_client.meta.region_name))

        for node_source in nodes:
            for node_destination in nodes:
                # skip self-access checks
                if node_source == node_destination:
                    continue

                # check if source is an admin, if so it can access destination but this is not tracked via an Edge
                if node_source.is_admin:
                    continue

                # check that destination is a role
                if ':role/' not in node_destination.arn:
                    continue

                # check that the destination role can be assumed by Lambda
                sim_result = resource_policy_authorization(
                    'lambda.amazonaws.com',
                    arns.get_account_id(node_source.arn),
                    node_destination.trust_policy,
                    'sts:AssumeRole',
                    node_destination.arn,
                    {},
                    debug
                )

                if sim_result != ResourcePolicyEvalResult.SERVICE_MATCH:
                    continue  # Lambda wasn't auth'd to assume the role

                # check that source can pass the destination role (store result for future reference)
                can_pass_role, need_mfa_passrole = query_interface.local_check_authorization_handling_mfa(
                    node_source,
                    'iam:PassRole',
                    node_destination.arn,
                    {
                        'iam:PassedToService': 'lambda.amazonaws.com'
                    },
                    debug
                )

                # check that source can create a Lambda function and pass it an execution role
                if can_pass_role:
                    can_create_function, need_mfa_0 = query_interface.local_check_authorization_handling_mfa(
                        node_source,
                        'lambda:CreateFunction',
                        '*',
                        {},
                        debug
                    )
                    if can_create_function:
                        if need_mfa_0 or need_mfa_passrole:
                            reason = '(requires MFA) can use Lambda to create a new function with arbitrary code, ' \
                                     'then pass and access'
                        else:
                            reason = 'can use Lambda to create a new function with arbitrary code, then pass and access'
                        new_edge = Edge(
                            node_source,
                            node_destination,
                            reason
                        )
                        output.write('Found new edge: {}\n'.format(new_edge.describe_edge()))
                        result.append(new_edge)

                # List of (<function>, bool, bool, bool)
                func_data = []
                for func in function_list:
                    can_change_code, need_mfa_1 = query_interface.local_check_authorization_handling_mfa(
                        node_source,
                        'lambda:UpdateFunctionCode',
                        func['FunctionArn'],
                        {},
                        debug
                    )
                    can_change_config, need_mfa_2 = query_interface.local_check_authorization_handling_mfa(
                        node_source,
                        'lambda:UpdateFunctionConfiguration',
                        func['FunctionArn'],
                        {},
                        debug
                    )
                    func_data.append((func, can_change_code, can_change_config, need_mfa_passrole or need_mfa_1 or need_mfa_2))

                # check that source can modify a Lambda function and use its existing role
                for func, can_change_code, can_change_config, need_mfa in func_data:
                    if node_destination.arn == func['Role']:
                        if can_change_code:
                            if need_mfa:
                                reason = '(requires MFA) can use Lambda to edit an existing function ({}) to access'.format(
                                    func['FunctionArn']
                                )
                            else:
                                reason = 'can use Lambda to edit an existing function ({}) to access'.format(
                                    func['FunctionArn']
                                )
                            new_edge = Edge(
                                node_source,
                                node_destination,
                                reason
                            )
                            output.write('Found new edge: {}\n'.format(new_edge.describe_edge()))
                            break

                # check that source can modify a Lambda function and pass it another execution role
                for func, can_change_code, can_change_config, need_mfa in func_data:
                    if can_change_config and can_change_code and can_pass_role:
                        if need_mfa:
                            reason = '(requires MFA) can use Lambda to edit an existing function ({}) to access'.format(
                                func['FunctionArn']
                            )
                        else:
                            reason = 'can use Lambda to edit an existing function ({}) to access'.format(
                                func['FunctionArn']
                            )
                        new_edge = Edge(
                            node_source,
                            node_destination,
                            reason
                        )
                        output.write('Found new edge: {}\n'.format(new_edge.describe_edge()))
                        break

        return result
