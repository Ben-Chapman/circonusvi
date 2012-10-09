#!/usr/bin/env python
import collections
import getopt
import json
import os
import pickle
import subprocess
import re
import sys
import tempfile

from circonusapi import circonusapi
from circonusapi import config

conf = config.load_config()
if not conf.has_section('circonusvi'):
    conf.add_section('circonusvi')

options = {
    'cache_file': '~/.circonusvi.cache',
    'add_comments': True,
    'debug': False,
    'endpoints': [],
    'editor': os.environ.get('EDITOR', 'vi'),
    'include_underscore': False,
    'reuse_last_query': False
}

# Allow overriding of any option in the config file
for k in options:
    if conf.has_option('circonusvi', k):
        if type(options[k] == bool):
            options[k] = conf.getboolean('circonusvi', k)
        else:
            options[k] = conf.get('circonusvi', k)

# Find the default account
options['account'] = conf.get('general', 'default_account')

class Enum(set):
    def __getattr__(self, name):
        if name in self:
            return name
        raise AttributeError

actions = Enum(["REEDIT", "PROCEED", "EXIT"])

def usage():
    print "Usage:"
    print sys.argv[0], "[options] [PATTERN]"
    print
    print "Pattern should be of the form key=pattern, where key is what you"
    print "want to match on (such as target), and pattern is a regular"
    print "expression. If any returned piece of data doesn't have the key"
    print "(e.g. rules don't have targets), then it doesn't match"
    print
    print "  -a -- Specify which account to use"
    print "  -c -- Don't resolve /broker/XXXX and add json 'comments'"
    print "  -d -- Enable debug mode"
    print "  -e -- Specify endpoints to search (can be used multiple times)"
    print "  -E -- Specify an alternate editor to use (default: $EDITOR)"
    print "  -l -- Don't query the API. Instead use the previous query results"
    print "  -u -- include underscore entries (e.g. _cid) in json output"

def confirm(text="OK to continue?"):
    response = None
    while response not in ['Y', 'y', 'N', 'n']:
        response = raw_input("%s (y/n) " % text)
    if response in ['Y', 'y']:
        return True
    return False

def parse_options():
    try:
        opts, args = getopt.gnu_getopt(sys.argv[1:], "a:cde:E:lu?")
    except getopt.GetoptError, err:
        # print help information and exit:
        print str(err) # will print something like "option -a not recognized"
        usage()
        sys.exit(2)

    for o,a in opts:
        if o == '-a':
            options['account'] = a
        if o == '-c':
            options['add_comments'] = not options['add_comments']
        if o == '-d':
            options['debug'] = not options['debug']
        if o == '-e':
            options['endpoints'].append(a)
        if o == '-E':
            options['editor'] = a
        if o == '-l':
            options['reuse_last_query'] = not options['reuse_last_query']
        if o == '-u':
            options['include_underscore'] = not options['include_underscore']
        if o == '-?':
            usage()
            sys.exit(0)
    return args

def get_api():
    token = conf.get('tokens', options['account'], None)
    api = circonusapi.CirconusAPI(token)
    if options['debug']:
        api.debug = True
    return api

def get_circonus_data(api):
    if not options['endpoints']:
        options['endpoints'] = ['check_bundle']

    # Combined output to be serialized
    data = {}

    for t in options['endpoints']:
        data.update(dict(((i['_cid'], i) for i in api.api_call("GET", t))))

def filter_circonus_data(data, args):
    # Filter based on the pattern
    patterns = []
    for p in args:
        parts = p.split('=', 1)
        if len(parts) != 2:
            print "Invalid pattern: %s" % p
            usage()
            sys.exit(2)
        patterns.append(parts)

    filtered_data = {}
    for i in data:
        for k, p in patterns:
            if k not in data[i]:
                break
            if not re.search(p, data[i][k]):
                break
        else:
            filtered_data[i] = data[i]
    return filtered_data

def load_cache(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename, "rb") as fh:
        return pickle.load(fh)

def save_cache(filename, cache):
    with open(filename, "wb") as fh:
        pickle.dump(cache, fh, pickle.HIGHEST_PROTOCOL)

def update_cache(api, cache, endpoint):
    data = cache.setdefault(endpoint, {})
    data.update(dict(((i['_cid'], i) for i in api.api_call("GET", endpoint))))

def get_cache(api, cache, endpoint, value):
    if endpoint not in cache or value not in cache[endpoint]:
        update_cache(api, cache, endpoint)
    if value in cache[endpoint]:
        return cache[endpoint][value]
    else:
        return None

def set_cache(cache, key, value):
    cache[key] = value

def get_cache_raw(cache, key):
    return cache.get(key, None)

def add_human_readable_comments(api, cache, filename):
    # Which endpoints do we resolve, and what are the human readable names?
    endpoints = {
        "broker": "_name",
        "user": "email"
    }
    fh = open(filename)
    lines = fh.readlines()
    fh.close()
    for i in range(0, len(lines)):
        for e in endpoints:
            match = re.search("\"(/%s/[0-9]+)\"" % e, lines[i])
            if match:
                lines[i] = "%s# %s\n%s" % (
                        re.match("^( *)", lines[i]).group(1),
                        get_cache(api, cache, e, match.group(1))[endpoints[e]],
                        lines[i])
    fh = open(filename, "w")
    fh.writelines(lines)
    fh.close()

def create_json_file(data):
    tmp = tempfile.mkstemp(suffix='.json')
    fh = os.fdopen(tmp[0], 'w')
    json.dump(data, fh, sort_keys=True, indent=4, separators=(',',': '))
    fh.close()
    return tmp[1]

def edit_json_file(filename):
    ok = False
    while not ok:
        subprocess.call([options['editor'], filename])
        fh = open(filename)
        data = []
        for line in fh:
            if re.match(" *#", line):
                continue
            data.append(line)
        fh.close()
        try:
            data_new = json.loads(''.join(data),
                    object_pairs_hook=json_pairs_hook_dedup_keys)
            ok = True
        except ValueError, e:
            print "Error parsing JSON:", e
            if not confirm("Do you want to edit the file again?"):
                sys.exit(1)
        fh.close()
    return data_new

def calculate_changes(data, data_new):
    changes = []
    for i in data_new:
        if i not in data:
            # Addition
            # We need to make sure the endpoint doesn't contain an ID (just in
            # case the entry contains one)
            changes.append({'action': 'POST', 'data': data_new[i],
                'endpoint': re.sub("(?!^)/.*", "", i)})
        elif data[i] != data_new[i]:
            # Edit
            changes.append({'action': 'PUT', 'data': data_new[i], 
                'data_old': data[i], 'endpoint': i})
    for i in data:
        if i not in data_new:
            # Delete
            changes.append({'action': 'DELETE', 'data': data[i], 'endpoint': i})
    return changes

def show_changes(changes):
    pretty_actions = {
        'DELETE': "Delete",
        'POST': "Addition",
        'PUT': "Edit"
    }
    for c in changes:
        print "Action: %s" % pretty_actions[c['action']]
        print "Endpoint: %s" % c['endpoint']
        if c['action'] == 'PUT':
            # We have an edit - show a diff
            print "Diff:"
            old_file = tempfile.mkstemp()
            new_file = tempfile.mkstemp()
            fh = os.fdopen(old_file[0], 'w')
            json.dump(c['data_old'], fh, sort_keys=True,
                    indent=4, separators=(',',': '))
            fh.close()
            fh = os.fdopen(new_file[0], 'w')
            json.dump(c['data'], fh, sort_keys=True,
                    indent=4, separators=(',',': '))
            fh.close()
            subprocess.call(["diff", "-u", old_file[1], new_file[1]])
            os.remove(old_file[1])
            os.remove(new_file[1])
        else:
            print "Data:"
            print json.dumps(c['data'], sort_keys=True,
                    indent=4, separators=(',',': '))

def confirm_changes(changes):
    if not changes:
        print "No Changes. Exiting."
        sys.exit(1)

    counts = collections.Counter((c['action'] for c in changes))

    print "%d additions, %d deletions, %d edits" % (
            counts['POST'], counts['DELETE'], counts['PUT'])
    response = None
    while True:
        response = raw_input("Do you want to proceed? (YyNnEeSs?) ")
        if response in ['Y', 'y']:
            return actions.PROCEED
        if response in ['N', 'n']:
            return actions.EXIT
        if response in ['E', 'e']:
            return actions.REEDIT
        if response in ['S', 's']:
            show_changes(changes)
        if response in ['?']:
            print "Y - Proceed"
            print "N - Quit"
            print "S - Show changes"
            print "E - Re-edit the file"
            print "? - Help"

def make_changes(changes):
    for c in changes:
        print "Making API Call: %s %s ..." % (c['action'], c['endpoint']),
        if c['action'] == 'DELETE':
            # We don't send any data along for deletions
            c['data'] = None
        try:
            api.api_call(c['action'], c['endpoint'], c['data'])
        except circonusapi.CirconusAPIError, e:
            print "Error"
            print "    %s" % e
            continue
        print "Success"

def strip_underscore_keys(data):
    for k in data.keys():
        for l in data[k].keys():
            if l[0] == '_':
                del data[k][l]

def json_pairs_hook_dedup_keys(data):
    # json decoder object_pairs_hook that allows duplicate keys, and makes
    # any duplicate keys unique by appending /x1, /x1 and so on to the end.
    # This is used when adding new items via the circonus api, you can just
    # specify /check_bundle multiple times as the endpoint and won't get an
    # error about duplicate keys when decoding the json. Separate code
    # elsewhere automatically strips off the /x1 when selecting the endpoint
    # to use for adding entries.
    d = {}
    ctr = 0
    for k,v in data:
        oldk = k
        while k in d:
            ctr += 1
            k = "%s/x%s" % (oldk, ctr)
        d[k] = v
    return d

if __name__ == '__main__':
    args = parse_options()
    api = get_api()
    cache_file = os.path.expanduser(options['cache_file'])
    cache = load_cache(cache_file)
    if options['reuse_last_query']:
        data = get_cache_raw(cache, '_last_query')
    else:
        data = get_circonus_data(api)
        set_cache(cache, '_last_query', data)
        save_cache(cache_file, cache)
    data = filter_circonus_data(data, args)
    if not options['include_underscore']:
        strip_underscore_keys(data)
    editing = True
    filename = create_json_file(data)
    if options['add_comments']:
        add_human_readable_comments(api, cache, filename)
        save_cache(cache_file, cache)
    while editing:
        data_new = edit_json_file(filename)
        editing = False
        changes = calculate_changes(data, data_new)
        next_action = confirm_changes(changes)
        if next_action == actions.REEDIT:
            editing = True
        elif next_action == actions.PROCEED:
            make_changes(changes)
        else:
            print "Not proceeding"
    os.remove(filename)
