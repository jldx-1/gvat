import sys

if sys.argv[1:] == ['--version']:
    from . import __version__
    print(f'gvat {__version__}')
elif sys.argv[1:] in (['--help'], ['-h']):
    from .arguments import help_parser
    help_parser().parse_args()
else:
    from .cli import main
    main()
