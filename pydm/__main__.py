import sys

if len(sys.argv) > 1:
    from .cli import main as cli_main

    sys.exit(cli_main())
else:
    from .gui import main

    main()
