..  _faq:

Frequently Asked Questions
**************************

..  _distribnotfound:

reprounzip shows errors with DistributionNotFound
=================================================

You probably have some plugins left over from a previous installation. Be sure to upgrade or remove outdated plugins when you upgrade reprounzip.

The following command might help::

    pip install -U reprounzip[all]

..  _moving-outputs:

My experiment's output files have no fixed path
===============================================

It is common for experiments to dynamically choose where the outputs should be written, for example by putting the date and time in the filename. However, ReproZip cannot understand these non-reproducible names; you need to put a single filename in the ``input_files`` sectin of your configuration file.

The easiest way to solve that is to write a simple bash script that runs your experiment, then renames the output to a known filename, or creates a symbolic link to it. You can then trace this script instead of the actual entry-point of your experiment, and specify that link as the path in the ``input_files`` dictionary.

..  _scp-py3:

I have trouble with *reprounzip-vagrant* on Python 3
====================================================

The *reprounzip-vagrant* plugin is compatible with Python 3, however the **scp.py** library used to transfer files has a number of issues. Until the maintainer accepts our patch, you can install our fixed version from Github using::

    pip install 'git+https://github.com/remram44/scp.py.git#egg=scp'