#!/usr/bin/env python

import sys
import ast
import time
import redis

py3 = sys.version_info[0] == 3

if py3:
    base_exception_class = Exception
else:
    base_exception_class = StandardError


class UnknownTypeError(base_exception_class):
    pass


class ConcurrentModificationError(base_exception_class):
    pass


# internal exceptions
class KeyDeletedError(base_exception_class):
    pass


class KeyTypeChangedError(base_exception_class):
    pass


def client(host='localhost', port=6379, password=None, db=0,
           unix_socket_path=None, encoding='utf-8'):
    if unix_socket_path is not None:
        r = redis.Redis(unix_socket_path=unix_socket_path,
                        password=password,
                        db=db,
                        charset=encoding)
    else:
        r = redis.Redis(host=host,
                        port=port,
                        password=password,
                        db=db,
                        charset=encoding)
    return r


def dumps(host='localhost', port=6379, password=None, db=0, pretty=False,
          unix_socket_path=None, encoding='utf-8'):
    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)
    kwargs = {}
    if not pretty:
        kwargs['separators'] = (',', ':')
    else:
        kwargs['indent'] = 2
        kwargs['sort_keys'] = True
    table = {}
    for key, type, expireat, value in _reader(r, pretty):
        table[key] = {'type': type, 'expireat': expireat, 'value': value}
    return repr(table)


def dump(fp, host='localhost', port=6379, password=None, db=0, pretty=False,
         unix_socket_path=None, encoding='utf-8'):
    fp.write(dumps(host=host, port=port, password=password, db=db,
                   pretty=pretty, encoding=encoding))


class StringReader(object):
    @staticmethod
    def send_command(p, key):
        p.get(key)

    @staticmethod
    def handle_response(response, pretty):
        # if key does not exist, get will return None;
        # however, our type check requires that the key exists
        return response


class ListReader(object):
    @staticmethod
    def send_command(p, key):
        p.lrange(key, 0, -1)

    @staticmethod
    def handle_response(response, pretty):
        return response


class SetReader(object):
    @staticmethod
    def send_command(p, key):
        p.smembers(key)

    @staticmethod
    def handle_response(response, pretty):
        response = list(response)
        if pretty:
            response.sort()
        return response


class ZsetReader(object):
    @staticmethod
    def send_command(p, key):
        p.zrange(key, 0, -1, False, True)

    @staticmethod
    def handle_response(response, pretty):
        return response


class HashReader(object):
    @staticmethod
    def send_command(p, key):
        p.hgetall(key)

    @staticmethod
    def handle_response(response, pretty):
        return response

readers = {
    'string': StringReader,
    'list': ListReader,
    'set': SetReader,
    'zset': ZsetReader,
    'hash': HashReader,
}


# note: key is a byte string
def _read_key(key, r, pretty):
    type = r.type(key)
    if type == 'none':
        # key was deleted by a concurrent operation on the data store
        raise KeyDeletedError
    reader = readers.get(type)
    if reader is None:
        raise UnknownTypeError("Unknown key type: %s" % type)
    p = r.pipeline()
    p.watch(key)
    p.multi()
    p.type(key)
    p.ttl(key)
    reader.send_command(p, key)
    # might raise redis.WatchError
    results = p.execute()
    actual_type = results[0]
    if actual_type != type:
        # type changed, retry
        raise KeyTypeChangedError
    ttl = results[1]
    expireat = int(time.time() + ttl) if ttl >= 0 else None
    value = reader.handle_response(results[2], pretty)
    return (type, expireat, value)


def _reader(r, pretty):
    for key in r.keys():
        handled = False
        for i in range(10):
            try:
                type, expireat, value = _read_key(key, r, pretty)
                yield key, type, expireat, value
                handled = True
                break
            except KeyDeletedError:
                # do not dump the key
                handled = True
                break
            except redis.WatchError:
                # same logic as key type changed
                pass
            except KeyTypeChangedError:
                # retry reading type again
                pass
        if not handled:
            # ran out of retries
            raise ConcurrentModificationError(
                'Key %s is being concurrently modified' % key)


def _empty(r):
    for key in r.keys():
        r.delete(key)


def loads(s, host='localhost', port=6379, password=None, db=0, empty=False,
          unix_socket_path=None, encoding='utf-8'):
    r = client(host=host, port=port, password=password, db=db,
               unix_socket_path=unix_socket_path, encoding=encoding)
    if empty:
        _empty(r)
    table = ast.literal_eval(s)
    counter = 0
    for key in table:
        # Create pipeline:
        if not counter:
            p = r.pipeline(transaction=False)
        item = table[key]
        type = item['type']
        expireat = item['expireat']
        value = item['value']
        _writer(p, key, type, expireat, value)
        # Increase counter until 10 000...
        counter = (counter + 1) % 10000
        # ... then execute:
        if not counter:
            p.execute()
    if counter:
        # Finally, execute again:
        p.execute()


def load(fp, host='localhost', port=6379, password=None, db=0, empty=False,
         unix_socket_path=None, encoding='utf-8'):
    s = fp.read()
    loads(s, host, port, password, db, empty, unix_socket_path, encoding)


def _writer(r, key, type, expireat, value):
    r.delete(key)
    if type == 'string':
        r.set(key, value)
    elif type == 'list':
        for element in value:
            r.rpush(key, element)
    elif type == 'set':
        for element in value:
            r.sadd(key, element)
    elif type == 'zset':
        for element, score in value:
            r.zadd(key, element, score)
    elif type == 'hash':
        r.hmset(key, value)
    else:
        raise UnknownTypeError("Unknown key type: %s" % type)
    if expireat:
        r.expireat(key, expireat)


if __name__ == '__main__':
    import optparse
    import os.path
    import re
    import sys

    DUMP = 1
    LOAD = 2

    def options_to_kwargs(options):
        args = {}
        if options.host:
            args['host'] = options.host
        if options.port:
            args['port'] = int(options.port)
        if options.socket:
            args['unix_socket_path'] = options.socket
        if options.password:
            args['password'] = options.password
        if options.db:
            args['db'] = int(options.db)
        if options.encoding:
            args['encoding'] = options.encoding
        # dump only
        if hasattr(options, 'pretty') and options.pretty:
            args['pretty'] = True
        # load only
        if hasattr(options, 'empty') and options.empty:
            args['empty'] = True
        return args

    def do_dump(options):
        if options.output:
            output = open(options.output, 'w')
        else:
            output = sys.stdout

        kwargs = options_to_kwargs(options)
        dump(output, **kwargs)

        if options.output:
            output.close()

    def do_load(options, args):
        if len(args) > 0:
            input = open(args[0], 'r')
        else:
            input = sys.stdin

        kwargs = options_to_kwargs(options)
        load(input, **kwargs)

        if len(args) > 0:
            input.close()

    script_name = os.path.basename(sys.argv[0])
    if re.search(r'load(?:$|\.)', script_name):
        action = help = LOAD
    elif re.search(r'dump(?:$|\.)', script_name):
        action = help = DUMP
    else:
        # default is dump, however if dump is specifically requested
        # we don't show help text for toggling between dumping and loading
        action = DUMP
        help = None

    if help == LOAD:
        usage = "Usage: %prog [options] [FILE]"
        usage += "\n\nLoad data from FILE (which must be a Python object dump previously created"
        usage += "\nby redisdl) into specified or default redis."
        usage += "\n\nIf FILE is omitted standard input is read."
    elif help == DUMP:
        usage = "Usage: %prog [options]"
        usage += "\n\nDump data from specified or default redis."
        usage += "\n\nIf no output file is specified, dump to standard output."
    else:
        usage = "Usage: %prog [options]"
        usage += "\n       %prog -l [options] [FILE]"
        usage += "\n\nDump data from redis or load data into redis."
        usage += "\n\nIf input or output file is specified, dump to standard output and load"
        usage += "\nfrom standard input."
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-H', '--host', help='connect to HOST (default localhost)')
    parser.add_option('-p', '--port', help='connect to PORT (default 6379)')
    parser.add_option('-s', '--socket', help='connect to SOCKET')
    parser.add_option('-w', '--password', help='connect with PASSWORD')
    if help == DUMP:
        parser.add_option('-d', '--db', help='dump DATABASE (0-N, default 0)')
        parser.add_option('-o', '--output', help='write to OUTPUT instead of stdout')
        parser.add_option('-y', '--pretty', help='Split output on multiple lines and indent it', action='store_true')
        parser.add_option('-E', '--encoding', help='set encoding to use while decoding data from redis', default='utf-8')
    elif help == LOAD:
        parser.add_option('-d', '--db', help='load into DATABASE (0-N, default 0)')
        parser.add_option('-e', '--empty', help='delete all keys in destination db prior to loading')
        parser.add_option('-E', '--encoding', help='set encoding to use while encoding data to redis', default='utf-8')
    else:
        parser.add_option('-l', '--load', help='load data into redis (default is to dump data from redis)', action='store_true')
        parser.add_option('-d', '--db', help='dump or load into DATABASE (0-N, default 0)')
        parser.add_option('-o', '--output', help='write to OUTPUT instead of stdout (dump mode only)')
        parser.add_option('-y', '--pretty', help='Split output on multiple lines and indent it (dump mode only)', action='store_true')
        parser.add_option('-e', '--empty', help='delete all keys in destination db prior to loading (load mode only)', action='store_true')
        parser.add_option('-E', '--encoding', help='set encoding to use while decoding data from redis', default='utf-8')
    options, args = parser.parse_args()

    if hasattr(options, 'load') and options.load:
        action = LOAD

    if action == DUMP:
        if len(args) > 0:
            parser.print_help()
            exit(4)
        do_dump(options)
    else:
        if len(args) > 1:
            parser.print_help()
            exit(4)
        do_load(options, args)
