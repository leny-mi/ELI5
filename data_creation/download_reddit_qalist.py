# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

### space-efficient download from https://files.pushshift.io/reddit/
import argparse
import bz2
import io
import json
import lzma
import os
import re
import requests
import subprocess
import urllib
import zstandard as zstd

from bs4 import BeautifulSoup
from glob import glob
from os.path import isfile
from os.path import join as pjoin
from time import sleep, time

from data_utils import *

REDDIT_URL  = "https://files.pushshift.io/reddit/"

# collects URLs for monthly dumps, has to be robust to file type changes
def gather_dump_urls(base_url, mode):
    page    = requests.get(base_url + mode)
    soup    = BeautifulSoup(page.content, 'lxml')
    files   = [it for it in soup.find_all(attrs={"class":"file"})]
    f_urls  = [tg.find_all(lambda x:x.has_attr('href'))[0]['href']
               for tg in files if len(tg.find_all(lambda x:x.has_attr('href'))) > 0]
    date_to_url    = {}
    for url_st in f_urls:
        ls  = re.findall(r"20[0-9]{2}-[0-9]{2}", url_st)
        if len(ls) > 0:
            yr, mt  = ls[0].split('-')
            date_to_url[(int(yr), int(mt))] = base_url + mode + url_st[1:]
    return date_to_url


# select valid top-level comments
def valid_comment(a):
    res = len(a['body'].split()) > 2 and \
          not a['body'].startswith('Your submission has been removed') and \
          a['author'] != 'AutoModerator' and a['parent_id'] == a['link_id']
    return res


# download a file, extract posts from desired subreddit, then remove from disk
def download_and_process(file_url, mode, subreddit_names, st_time):
    # download and pre-process original posts
    f_name  = pjoin('reddit_tmp', file_url.split('/')[-1])
    tries_left  = 4
    while tries_left:
        try:
            print("downloading %s %2f" % (f_name, time() - st_time))
            subprocess.run(['wget', '-P', 'reddit_tmp', file_url], stdout=subprocess.PIPE)
            print("decompressing and filtering %s %2f" % (f_name, time() - st_time))
            if f_name.split('.')[-1] == 'xz':
                f   = lzma.open(f_name, 'rt')
            elif f_name.split('.')[-1] == 'bz2':
                f   = bz2.open(f_name, 'rt')
            elif f_name.split('.')[-1] == 'zst':
                fh              = open(f_name, 'rb')
                dctx            = zstd.ZstdDecompressor(max_window_size=2147483648)
                stream_reader   = dctx.stream_reader(fh)
                f   = io.TextIOWrapper(stream_reader, encoding='utf-8')
            lines   = dict([(name, []) for name in subreddit_names])
            for i, l in enumerate(f):
                if i % 1000000 == 0:
                    print("read %d lines, found %d" % (i, sum([len(ls) for ls in lines.values()])), time() - st_time)
                for name in subreddit_names:
                    if name in l:
                        lines[name] += [l.strip()]
            if f_name.split('.')[-1] == 'zst':
                fh.close()
            else:
                f.close()
            os.remove(f_name)
            tries_left  = 0
        except EOFError as e:
            sleep(10)
            print("failed reading file %s file, another %d tries" % (f_name, tries_left))
            os.remove(f_name)
            tries_left  -= 1
    print("tokenizing and selecting %s %2f" % (f_name, time() - st_time))
    processed_items = dict([(name, []) for name in subreddit_names])
    if mode == 'submissions':
        key_list    = ['id', 'score', 'url', 'title', 'selftext']
    else:
        key_list    = ['id', 'link_id', 'parent_id', 'score', 'body']
    for name in subreddit_names:
        for line in lines[name]:
            reddit_dct  = json.loads(line)
            if reddit_dct.get('num_comments', 1) > 0 and reddit_dct.get('score', 0) and reddit_dct.get('score', 0) >= 2 and (mode == 'submissions' or valid_comment(reddit_dct)):
                reddit_res  = {}
                for k in key_list:
                    if k in ['title', 'selftext', 'body']:
                        if reddit_dct[k].lower() in ['[removed]', '[deleted]']:
                            reddit_dct[k]   = ''
                        txt, url_list       = word_url_tokenize(reddit_dct[k])
                        reddit_res[k]       = (' '.join(txt.split()), url_list)
                    else:
                        reddit_res[k]       = reddit_dct[k]
                processed_items[name] += [reddit_res]
    print("Total found %d" % (len(processed_items)), time() - st_time)
    return processed_items


def post_process(reddit_dct, name=''):
    # remove the ELI5 at the start of explainlikeimfive questions
    start_re    = re.compile('[\[]?[ ]?eli[5f][ ]?[\]]?[]?[:,]?', re.IGNORECASE)
    if name == 'explainlikeimfive':
        title, uls  = reddit_dct['title']
        title       = start_re.sub('', title).strip()
        reddit_dct['title'] = [title, uls]
    # dedupe and filter comments
    comments    = [c for i, c in enumerate(reddit_dct['comments']) if len(c['body'][0].split()) >= 8 and c['id'] not in [x['id'] for x in reddit_dct['comments'][:i]]]
    comments    = sorted(comments, key=lambda c: (c['score'], len(c['body'][0].split()), c['id']), reverse=True)
    reddit_dct['comments']  = comments
    return reddit_dct


def main():
    parser  = argparse.ArgumentParser(description='Subreddit QA pair downloader')
    parser.add_argument('-sy', '--start_year', default=2011, type=int, metavar='N',
                        help='starting year')
    parser.add_argument('-ey', '--end_year', default=2018, type=int, metavar='N',
                        help='end year')
    parser.add_argument('-sm', '--start_month', default=7, type=int, metavar='N',
                        help='starting year')
    parser.add_argument('-em', '--end_month', default=7, type=int, metavar='N',
                        help='end year')
    parser.add_argument('-sr_l', '--subreddit_list', default='["explainlikeimfive"]', type=str,
                        help='subreddit name')
    parser.add_argument('-Q', '--questions_only', action='store_true',
                        help= 'only download submissions')
    parser.add_argument('-A', '--answers_only', action='store_true',
                        help= 'only download comments')
    args        = parser.parse_args()
    ### collect submissions and comments monthly URLs
    date_to_url_submissions = gather_dump_urls(REDDIT_URL,
                                               "submissions")
    date_to_url_comments    = gather_dump_urls(REDDIT_URL,
                                               "comments")
    date_to_urls    = {}
    for k, v in date_to_url_submissions.items():
        date_to_urls[k]    = (v, date_to_url_comments.get(k, ''))
    ### download, filter, process, remove
    subprocess.run(['mkdir', 'reddit_tmp'], stdout=subprocess.PIPE)
    st_time    = time()
    subreddit_names = json.loads(args.subreddit_list)
    output_files    = dict([(name, "processed_data/%s_qalist.json" % (name,))
                            for name in subreddit_names])
    qa_dict         = dict([(name, {}) for name in subreddit_names])
    for name, fname in output_files.items():
        if isfile(fname):
            print("loading already processed documents from", fname)
            f = open(fname)
            qa_dict[name] = dict(json.load(f))
            f.close()
            print("loaded already processed documents")
    # slice file save
    n_months    = 0
    for year in range(args.start_year, args.end_year + 1):
        st_month    = args.start_month if year == args.start_year else 1
        end_month   = args.end_month if year == args.end_year else 12
        months      = range(st_month, end_month + 1)
        for month in months:
            merged_comments = 0
            submissions_url, comments_url   = date_to_urls[(year, month)]
            if not args.answers_only:
                try:
                    processed_submissions   = download_and_process(submissions_url,
                                                                   'submissions',
                                                                   subreddit_names,
                                                                   st_time)
                except FileNotFoundError as e:
                    sleep(60)
                    print("retrying %s once" % (submissions_url))
                    processed_submissions   = download_and_process(submissions_url,
                                                                   'submissions',
                                                                   subreddit_names,
                                                                   st_time)
                for name in subreddit_names:
                    for dct in processed_submissions[name]:
                        qa_dict[name][dct['id']]  = dct
            if not args.questions_only:
                try:
                    processed_comments      = download_and_process(comments_url,
                                                                   'comments',
                                                                   subreddit_names,
                                                                   st_time)
                except FileNotFoundError as e:
                    sleep(60)
                    print("retrying %s once" % (comments_url))
                    processed_comments      = download_and_process(comments_url,
                                                                   'comments',
                                                                   subreddit_names,
                                                                   st_time)
                # merge submissions and comments
                for name in subreddit_names:
                    merged_comments = 0
                    for dct in processed_comments[name]:
                        did = dct['parent_id'].split('_')[-1]
                        # did = dct['parent_id'][3:]
                        if did in qa_dict[name]:
                            merged_comments += 1
                            comments_list               = qa_dict[name][did].get('comments', []) + [dct]
                            qa_dict[name][did]['comments']    = sorted(comments_list,
                                                                 key=lambda x:x['score'],
                                                                 reverse=True)
                    print("----- added to global dictionary", name, year, month,
                                                              time() - st_time,
                                                              merged_comments,
                                                              len(qa_dict[name]))
            for name, out_file_name in output_files.items():
                fo = open(out_file_name, "w")
                json.dump([(eli_k, eli_dct) for eli_k, eli_dct in qa_dict[name].items()], fo)
                fo.close()
    if not args.questions_only:
        for name, out_file_name in output_files.items():
            print('post-processing', name)
            qa_dct_list = [(k, post_process(rdct, name)) for k, rdct in qa_dict[name].items() if 'comments' in rdct]
            qa_dct_list = [x for x in qa_dct_list if len(x[1]['comments']) > 0 and name in x[1]['url']]
            fo = open(out_file_name, "w")
            json.dump(qa_dct_list, fo)
            fo.close()


if __name__ == '__main__':
    main()
