#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# -----
#
# Certain portions of the contents of this file are derived from TPC-H version 3.0.1
# (retrieved from www.tpc.org/tpc_documents_current_versions/current_specifications5.asp).
# Such portions are subject to copyrights held by Transaction Processing Performance Council (“TPC”)
# and licensed under the TPC EULA (a copy of which accompanies this file as “TPC EULA” and is also
# available at http://www.tpc.org/tpc_documents_current_versions/current_specifications5.asp) (the “TPC EULA”).
#
# You may not use this file except in compliance with the TPC EULA.
# DISCLAIMER: Portions of this file is derived from the TPC-H Benchmark and as such any results
# obtained using this file are not comparable to published TPC-H Benchmark results, as the results
# obtained from using this file do not comply with the TPC-H Benchmark.
#

import argparse
import glob
import json
import math
import os
import re
import time
from decimal import Decimal

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import DoubleType, FloatType
from pyspark.sql.functions import col

from nds_h_power import gen_sql_from_stream, get_query_subset

SKIP_QUERIES = [
    'query15_part1', # create view query
    'query15_part3', # drop view query
]
SKIP_COLUMNS = {
    'query18': ['o_orderkey'], # non-deterministic output: https://github.com/NVIDIA/spark-rapids-benchmarks/pull/198#issuecomment-2403837688
}


def compare_results(spark_session: SparkSession,
                    input1: str,
                    input2: str,
                    input1_format: str,
                    input2_format: str,
                    ignore_ordering: bool,
                    query_name: str,
                    use_iterator=False,
                    max_errors=10,
                    epsilon=0.00001) -> bool:
    """Giving 2 paths of input query output data, compare them row by row, value by value to see if
    the results match or not.

    Args:
        spark_session (SparkSession): Spark Session to hold the comparison
        input1 (str): path for the first input data
        input2 (str): path for the second input data
        input1_format (str): data source format for input1, e.g. parquet, orc
        input2_format (str): data source format for input2, e.g. parquet, orc
        ignore_ordering (bool): whether ignoring the order of input data.
            If true, we will order by ourselves.
        query_name (str): Query name.
        use_iterator (bool, optional): When set to true, use `toLocalIterator` to load one partition
            at a time into driver memory, reducing memory usage at the cost of performance because
            processing will be single-threaded. Defaults to False.
        max_errors (int, optional): Maximum number of differences to report. Defaults to 10.
        epsilon (float, optional): Allow for differences in precision when comparing floating point
            values. Defaults to 0.00001.

    Returns:
        bool: True if result matches otherwise False
    """
    if query_name in SKIP_QUERIES:
        return True

    df1 = spark_session.read.format(input1_format).load(input1)
    df2 = spark_session.read.format(input2_format).load(input2)
    count1 = df1.count()
    count2 = df2.count()

    if(count1 == count2):
        #TODO: need partitioned collect for NDS? there's no partitioned output currently
        result1 = collect_results(df1, query_name, ignore_ordering, use_iterator)
        result2 = collect_results(df2, query_name, ignore_ordering, use_iterator)

        errors = 0
        i = 0
        while i < count1 and errors < max_errors:
            lhs = next(result1)
            rhs = next(result2)
            if not rowEqual(list(lhs), list(rhs), epsilon):
                print(f"Row {i}: \n{list(lhs)}\n{list(rhs)}\n")
                errors += 1
            i += 1
        print(f"Processed {i} rows")
        
        if errors == max_errors:
            print(f"Aborting comparison after reaching maximum of {max_errors} errors")
            return False
        elif errors == 0:
            print("Results match")
            return True
        else:
            print(f"There were {errors} errors")
            return False
    else:
        print(f"DataFrame row counts do not match: {count1} != {count2}")
        return False

def collect_results(df: DataFrame,
                    query_name: str,
                    ignore_ordering: bool,
                    use_iterator: bool):
    # skip output for specific query columns
    if query_name in SKIP_COLUMNS:
        df = df.drop(*SKIP_COLUMNS[query_name])

    # apply sorting if specified
    non_float_cols = [col(field.name) for field in df.schema.fields 
                      if field.dataType.typeName() not in (FloatType.typeName(), DoubleType.typeName())]
    float_cols = [col(field.name) for field in df.schema.fields
                  if field.dataType.typeName() in (FloatType.typeName(), DoubleType.typeName())]
    if ignore_ordering:
        df = df.sort(non_float_cols + float_cols)

    # TODO: do we still need this for NDS? Query outputs are usually 1 - 100 rows,
    #       there should'nt be memory pressure.
    if use_iterator:
        it = df.toLocalIterator()
    else:
        print("Collecting rows from DataFrame")
        t1 = time.time()
        rows = df.collect()
        t2 = time.time()
        print(f"Collected {len(rows)} rows in {t2-t1} seconds")
        it = iter(rows)
    return it


def rowEqual(row1, row2, epsilon):
    # only simple types in a row for NDS results
    return all([compare(lhs, rhs, epsilon) for lhs, rhs in zip(row1, row2)])
        

def compare(expected, actual, epsilon=0.00001):
    #TODO 1: we can optimize this with case-match after Python 3.10
    #TODO 2: we can support complex data types like nested type if needed in the future.
    #        now NDS only contains simple data types.
    if isinstance(expected, float) and isinstance(actual, float):
        # Double is converted to float in pyspark...
        if math.isnan(expected) and math.isnan(actual):
            return True
        return math.isclose(expected, actual, rel_tol=epsilon)

    if isinstance(expected, Decimal) and isinstance(actual, Decimal):
        return math.isclose(expected, actual, rel_tol=epsilon)

    return expected == actual

def iterate_queries(spark_session: SparkSession,
                    input1: str,
                    input2: str,
                    input1_format: str,
                    input2_format: str,
                    ignore_ordering: bool,
                    query_dict: dict,
                    use_iterator=False,
                    max_errors=10,
                    epsilon=0.00001):
    # Iterate each query folder for a Power Run output
    # Providing a list instead of hard-coding all queires is to satisfy the arbitary queries run.
    unmatch_queries = []
    for query_name in query_dict.keys():
        sub_input1 = input1 + '/' + query_name
        sub_input2 = input2 + '/' + query_name
        print(f"=== Comparing Query: {query_name} ===")
        result_equal = compare_results(spark_session,
                                       sub_input1,
                                       sub_input2,
                                       input1_format,
                                       input2_format,
                                       ignore_ordering,
                                       query_name,
                                       use_iterator=use_iterator,
                                       max_errors=max_errors,
                                       epsilon=epsilon)
        if result_equal == False:
            unmatch_queries.append(query_name)
    if len(unmatch_queries) != 0:
        print(f"=== Unmatch Queries: {unmatch_queries} ===")
    return unmatch_queries

def update_summary(prefix, unmatch_queries):
    """set the queryValidationStatus field in json summary file.
    If the queryStatus is 'Completed' or 'CompletedWithTaskFailures' but validation failed,
    set to 'Fail'.
    If the queryStatus is 'Completed' or 'CompletedWithTaskFailures' and validation passed,
    set to 'Pass'.
    If the queryStatus is 'Failed',
    set to 'NotAttempted'.

    Args:
        prefix (str): folder of the json summary files
        unmatch_queries ([str]): list of queries that failed validation
    """
    if not os.path.exists(prefix):
        raise Exception("The json summary folder doesn't exist.")
    print(f"Updating queryValidationStatus in folder {prefix}.")
    for query_name in query_dict.keys():
        summary_wildcard = prefix + f'/*{query_name}-*.json'
        file_glob = glob.glob(summary_wildcard)

        # Expect only one summary file for each query
        if len(file_glob) > 1:
            raise Exception(f"More than one summary file found for query {query_name} in folder {prefix}.")
        if len(file_glob) == 0:
            raise Exception(f"No summary file found for query {query_name} in folder {prefix}.")
        
        filename = file_glob[0]
        with open(filename, 'r') as f:
            summary = json.load(f)
            if query_name in unmatch_queries:
                if 'Completed' in summary['queryStatus'] or 'CompletedWithTaskFailures' in summary['queryStatus']:
                    summary['queryValidationStatus'] = ['Fail']
                else:
                    summary['queryValidationStatus'] = ['NotAttempted']
            else:
                summary['queryValidationStatus'] = ['Pass']
        with open(filename, 'w') as f:
            json.dump(summary, f, indent=2)

if __name__ == "__main__":
    parser = parser = argparse.ArgumentParser()
    parser.add_argument('input1',
                        help='path of the first input data.')
    parser.add_argument('input2',
                        help='path of the second input data.')
    parser.add_argument('query_stream_file',
                        help='query stream file that contains NDS queries in specific order.')
    parser.add_argument('--input1_format',
                        default='parquet',
                        help='data source type for the first input data. e.g. parquet, orc. Default is: parquet.')
    parser.add_argument('--input2_format',
                        default='parquet',
                        help='data source type for the second input data. e.g. parquet, orc. Default is: parquet.')
    parser.add_argument('--max_errors',
                        help='Maximum number of differences to report.',
                        type=int,
                        default=10)
    parser.add_argument('--epsilon',
                        type=float,
                        default=0.00001,
                        help='Allow for differences in precision when comparing floating point values.' +
                        ' Given 2 float numbers: 0.000001 and 0.000000, the diff of them is 0.000001' +
                        ' which is less than 0.00001, so we regard this as acceptable and will not' +
                        ' report a mismatch.')
    parser.add_argument('--ignore_ordering',
                        action='store_true',
                        help='Sort the data collected from the DataFrames before comparing them.')
    parser.add_argument('--use_iterator',
                        action='store_true',
                        help='When set, use `toLocalIterator` to load one partition at a' +
                        ' time into driver memory, reducing memory usage at the cost of performance' +
                        ' because processing will be single-threaded.')
    parser.add_argument('--json_summary_folder',
                        help='path of a folder that contains json summary file for each query.')
    parser.add_argument('--sub_queries',
                        type=lambda s: [x.strip() for x in s.split(',')],
                        help='comma separated list of queries to compare. If not specified, all queries ' +
                        'in the stream file will be compared. e.g. "query1,query2,query3". Note, use ' +
                        '"_part1" and "_part2" suffix e.g. query15_part1, query15_part2')
    args = parser.parse_args()
    query_dict = gen_sql_from_stream(args.query_stream_file)
    # if set sub_queries, only compare the specified queries
    if args.sub_queries:
        query_dict = get_query_subset(query_dict, args.sub_queries)
    session_builder = SparkSession.builder.appName("Validate Query Output").getOrCreate()
    unmatch_queries = iterate_queries(session_builder,
                                      args.input1,
                                      args.input2,
                                      args.input1_format,
                                      args.input2_format,
                                      args.ignore_ordering,
                                      query_dict,
                                      use_iterator=args.use_iterator,
                                      max_errors=args.max_errors,
                                      epsilon=args.epsilon)
    if args.json_summary_folder:
        update_summary(args.json_summary_folder, unmatch_queries)
