# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
# <p/>
# http://www.apache.org/licenses/LICENSE-2.0
# <p/>
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
####
#
# A simple client to sync data from solr to elastic.
# The parameters are configurable via a config file.
# An example config file:
"""
[solr]
    url=http://user:pass@host/solr/core
    query=*:*
    fl=*
    start=0
    rows=100
    # rows is page size, the client internally paginates over all docs matched to query
    limit=
    # limit is for terminating after N number of docs
[import]
    log_delay=2000
[dump]
    filename = dump-1.json
"""
# Author : Thamme Gowda
# Date   : March 04, 2016

from argparse import ArgumentParser
from configparser import ConfigParser
import requests
import time
import re
from datetime import datetime
import os.path
import codecs
import traceback                    
import json                    


current_milli_time = lambda: int(round(time.time() * 1000))  # replacement for System.currentMillis() ;)


class Solr(object):

    DT_FMT = "%Y-%m-%dT%H:%M:%SZ"

    '''
    Solr client for querying docs
    '''
    def __init__(self, solr_url):
        self.query_url = solr_url.rstrip('/') + '/' + 'select'

    def query(self, query='*:*', start=0, rows=20, **kwargs):
        '''
        Queries solr and returns results as a dictionary
        returns None on failure, items on success
        '''
        payload = {
            'q': query,
            'wt': 'python',
            'start': start,
            'rows': rows
        }
        if kwargs:
            for key in kwargs:
                payload[key] = kwargs.get(key)

        resp = requests.get(self.query_url, params=payload)
        if resp.status_code == 200:
            return eval(resp.text)['response']['docs']
        else:
            print(resp.status_code)
            return None

    def query_iterator(self, query='*:*', start=0, rows=20, limit=None, **kwargs):
        '''
        Queries solr server and returns Solr response  as dictionary
        returns None on failure, iterator of results on success
        '''
        payload = {
            'q': query,
            'wt': 'python',
            'rows': rows
        }

        if kwargs:
            for key in kwargs:
                payload[key] = kwargs.get(key)

        total = start + 1
        count = 0
        while start < total:
            if limit and count >= limit: # terminate
                break
            payload['start'] = start
            print('start = %s, total= %s' % (start, total))
            resp = requests.get(self.query_url, params=payload)
            if not resp:
                print('no response from solr server!')
                break

            if resp.status_code == 200:
                resp = eval(resp.text)
                total = resp['response']['numFound']
                for doc in resp['response']['docs']:
                    start += 1
                    count += 1
                    yield doc
            else:
                print(resp)
                print('Oops! Some thing went wrong while querying solr')
                print('Solr query params = %s', payload)
                break

class SolrDumper(object):
    """
    A client for Importing data from solr to elastic search
    """

    def __init__(self, config):
        self.config = config
        self.solr = Solr(config.get('solr', 'url'))

    def get_parent_id(self, id):
        """
        Gets parent document of a doc
        :param id:  parent document
        :return: id of parent if exists, else None
        """
        qry = 'outpaths:"%s"' % id
        parents = self.solr.query(qry, start=0, rows=3, fl="id")
        if parents:
            for parent in parents:
                if id != parent['id']: # the same document may be in parent list!
                    return parent['id']
        return None

    def dump(self, transform_func):
        """
        Syncs data from solr to elastic
        :param transform_func: function to transform solr document to elastic document
        :return: num docs processed
        """
        qry = self.config.get('solr','query')
        start = int(self.config.get('solr', 'start'))
        rows = int(self.config.get('solr', 'rows'))
        limit = self.config.get('solr', 'limit')
        dumpfile = self.config.get('dump', 'filename')
        if not dumpfile:
            raise Exception("Dump file not specified in config")
        if limit:
            limit = int(limit)

        fl = self.config.get('solr', 'fl')
        docs = self.solr.query_iterator(query=qry, start=start, rows=rows, limit=limit, fl=fl)

        buffer = []

        st = current_milli_time()
        progress_delay = int(self.config.get('import', 'log_delay'))
        count = 0
        num_batches = 0
        f = open(dumpfile, 'w')
        for solr_doc in docs:
            count += 1
            if not 'parent_id' in solr_doc:   #get it
                solr_doc['parent_id'] = self.get_parent_id(solr_doc['id'])
            (id, es_doc) = transform_func(solr_doc)
            es_doc['imported_at'] = current_milli_time()
            es_doc['_id'] = id
            
            f.write(json.dumps(es_doc))
            f.write('\n')

            if current_milli_time() - st > progress_delay:
                print("%d ,Batch:%d LastDoc:%s" %(count, num_batches, id))
                st = current_milli_time()

        if len(buffer) > 0:
            helpers.bulk(self.elastic, buffer)
            pass
        print("Done: %d" % count)
        return count
        f.close()

def transform_edr2cdr(doc):
    """
    This function transforms EDR(aka Solr) doc to CDR (aka Elastic)
    :param doc: Solr document
    :return: (id, elastic_doc)
    """
    id = doc['id']
    res = {}
    metadata = {} # since ES can take nested json, we club all metadata keys from solr
    metadata['edr_id'] = id
    for key, val in doc.items():
        if key in Config.mapping:
            res[Config.mapping[key]] = val
            continue
        match = Config.md_pattern.match(key)
        if match:
            metadata[match.group(1)] = val
        elif key not in Config.removals:
            metadata[key] = val     # move it to extracted metadata
    res['extracted_metadata'] = metadata
    res['obj_stored_url'] = id.replace(Config.dump_path, Config.mount_point)
    try:
        res['timestamp'] = 1000 * int(parse_date(doc.get('lastModified')).strftime("%s"))
    except Exception as e:
        print("Skipped timestamp: Error %s" % e)
    res.update(Config.additions)
    if "text" in doc['contentType'] or "ml" in doc['contentType']: # for text content type
        res['raw_content'] = get_raw_content(id.replace("file:", ""))

    if 'outlinks' in metadata:
        metadata['obj_outlinks'] = metadata['outlinks']
        del metadata['outlinks']
    if 'outpaths' in metadata:
        metadata['obj_children'] = transform_edr2cdr_id(metadata['outpaths'])
        del metadata['outpaths']
    # Map ids to CDR
    id = transform_edr2cdr_id(res.get("obj_id"))
    res['obj_id'] = id
    res['obj_parent'] = transform_edr2cdr_id(res.get("obj_parent"))

    # res['crawl_data'] = None FIXME: these are not found in solr
    return (id, res)

def transform_edr2cdr_id(edrid):
    """
    Converts edr id to cdr id.
    EDR id includes coplete path of file,
    CDR id just has has hash which is also a file name
    :param edrid:
    :return: file name
    """
    if not edrid:
        return None
    if type(edrid) == list:
        return map(transform_edr2cdr_id, edrid)
    else:
        return edrid.split("/")[-1]

def get_raw_content(path):
    """
    Gets content of a file as string
    :param path: file path
    :return: content as string
    """
    if os.path.isfile(path):
        try :
            with codecs.open(path, encoding='utf-8', errors="ignore") as f:
                return f.read()
        except Exception as e:
            print("Error reading %s :: %s" %(path, e))
    return None

 
def parse_date(date_str, fmt=Solr.DT_FMT):
    """
    parses date string to date object
    :param date_str: date string in solr format
    :return: date object on success or datetime.now() on failure
    """
    if date_str:
        try:
            return datetime.strptime(date_str, fmt)
        except Exception:
            print("Error parsing date %s" % date_str)
    return datetime.now()


class Config(object):
    """
    transformation config
    """

    additions = {
        'crawler':'Nutch-1.12-SNAPSHOT',
        'team': 'NASA_JPL',
        'version':2.0
    }
    mapping = {
        'id': 'obj_id',
        'parent_id': 'obj_parent',
        'contentType': 'content_type',
        'content': 'extracted_text',
        'url': 'obj_original_url'
    }
    removals = {}
    md_pattern = re.compile(r"(.*)_(ts?|ss?|ds?|bs?|fs?|is?|ls?)_md")
    dump_path = "file:/data2/USCWeaponsStatsGathering/nutch/full_dump/"
    mount_point = "http://imagecat.dyndns.org/weapons/alldata/"

if __name__ == '__main__':
    parser = ArgumentParser(description="This program copies data from EDR(Solr) to CDR(Elastic)" +
                                        "\n and was developed at NASA JPL to copy index from " +
                                        "solr to Elastic Search")
    parser.add_argument('-cfg','--config', help='Configuration File', required=True)
    args = vars(parser.parse_args())
    config = ConfigParser()
    config.read(args['config'])
    dumper = SolrDumper(config)
    count = dumper.dump(transform_edr2cdr)
    print("Total docs imported=%d" % count)
