#!/usr/bin/env python3
"""Opt-in native OpenHop enrollment entry point. Importing starts nothing.

--check only checks dependency imports. It reads no credential or configuration
contents and opens no socket/radio. Normal startup requires all explicit paths.
"""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--enrollment-ca-cert', type=Path)
    parser.add_argument('--enrollment-ca-key', type=Path)
    parser.add_argument('--enrollment-state-dir', type=Path)
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args()
    import cryptography
    import yaml
    from private_link import transport
    from enrollment_server import EnrollmentController, EnrollmentPeerService
    import repeater
    import openhop_core
    if args.check:
        import repeater.main
        print('Native enrollment imports passed. No configuration, credential, network or radio operation occurred.')
        print('cryptography', cryptography.__version__)
        return
    for field in ('config', 'enrollment_ca_cert', 'enrollment_ca_key', 'enrollment_state_dir'):
        value = getattr(args, field)
        if value is None or not value.is_absolute():
            parser.error(field.replace('_', '-') + ' requires an explicit absolute path')
    # Record the reviewed SACMesh bundle while preserving upstream base numbers.
    repeater.__version__ = '1.1.5.dev179+g61e5ffa.ilinkui20261105'
    openhop_core.__version__ = '1.1.4.dev6+g61e5ffa.ilinkui20261105'
    import repeater.main
    # Normal startup uses the explicitly approved activation paths.
    controller = EnrollmentController(args.config, args.enrollment_ca_cert,
                                      args.enrollment_ca_key, args.enrollment_state_dir)
    transport.create_service = lambda settings: EnrollmentPeerService(settings, controller)
    sys.argv = [sys.argv[0], '--config', str(args.config)]
    if args.log_level:
        sys.argv.extend(['--log-level', args.log_level])
    repeater.main.main()


if __name__ == '__main__':
    main()
