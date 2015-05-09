#!/bin/sh

set -eux

case "$TEST_MODE"
in
    coverage)
        # Python
        find . -name '.coverage*'
        if [ -f .coverage ]; then mv .coverage .coverage.orig; fi # FIXME: useless?
        coverage combine
        find . -name '.coverage*'
        python -c "import coveralls.cli; coveralls.cli.main()"

        # C
        python -c "import cpp_coveralls; cpp_coveralls.run()" --verbose --build-root "$PWD/reprozip"
        ;;
esac
