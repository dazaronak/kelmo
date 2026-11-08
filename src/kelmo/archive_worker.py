"""Restricted archive process entry point, started by the coordinator inside OS confinement.

It runs with Python -I -S: the parent names the read-only import roots it may use, and
the startup frame on stdin carries the enrollment and the archive paths. The worker logic
itself is kelmo.server.archive_worker_main.
"""
import json
import sys


def main(argv):
    roots, confined = [], False
    arguments = iter(argv)
    for argument in arguments:
        if argument == '--confined':
            confined = True
        elif argument == '--import-root':
            roots.append(next(arguments, ''))
        else:
            roots = None
            break
    if roots is None or not all(root.startswith('/') for root in roots) or not sys.flags.isolated \
            or not sys.flags.no_site:
        sys.stdout.write(json.dumps({'ready': False, 'error': {
            'code': 'E_ISOLATION', 'message': 'The archive process requires isolated Python and absolute import roots.'}}) + '\n')
        sys.stdout.flush()
        return 2
    sys.path[:0] = roots
    from kelmo.server import archive_worker_main, protocol_streams
    stdin, stdout = protocol_streams()
    return archive_worker_main(stdin, stdout, confined=confined)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
