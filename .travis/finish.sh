#!/bin/sh

set -eux

case "$TEST_MODE"
in
    coverage)
        # Python
        if [ -f .coverage ]; then mv .coverage .coverage.orig; fi # FIXME: useless?
        coverage combine
        python -c "import coveralls.cli; coveralls.cli.main()"

        # C
        python -c "import cpp_coveralls; cpp_coveralls.run()" --verbose --build-root "$PWD/reprozip"
        ;;
esac
