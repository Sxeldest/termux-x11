#!/usr/bin/env python3
"""Minimal CLI code-assistant agent (prototype).

Usage:
  python agent/agent.py scan   # scan repo and print summary
  python agent/agent.py info   # print agent info
"""
import argparse
import os
import sys


def scan_repo(root):
    counts = {}
    sizes = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip .git and build folders
        if any(part in ('.git', 'build', 'gradle') for part in dirpath.split(os.sep)):
            continue
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower() or '<noext>'
            counts[ext] = counts.get(ext, 0) + 1
            try:
                sizes.append((os.path.getsize(path), path))
            except OSError:
                pass

    print('File type counts:')
    for ext, c in sorted(counts.items(), key=lambda x: -x[1])[:20]:
        print(f'  {ext:8} {c:6d}')

    print('\nTop 10 largest files:')
    for size, path in sorted(sizes, reverse=True)[:10]:
        print(f'  {size:10d}  {path}')


def info():
    print('Agent: Minimal Code Assistant (prototype)')
    print('Capabilities: repo scanning, simple helpers, scaffold hooks')


def main(argv=None):
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(prog='agent')
    sub = parser.add_subparsers(dest='cmd')

    sub.add_parser('scan', help='Scan repository and show a summary')
    sub.add_parser('info', help='Show agent info')

    args = parser.parse_args(argv)
    if args.cmd == 'scan':
        scan_repo(os.getcwd())
    elif args.cmd == 'info':
        info()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
