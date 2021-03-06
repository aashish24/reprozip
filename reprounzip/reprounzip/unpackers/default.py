# Copyright (C) 2014-2015 New York University
# This file is part of ReproZip which is released under the Revised BSD License
# See file LICENSE for full license details.

"""Default unpackers for reprounzip.

This file contains the default plugins that come with reprounzip:
- ``directory`` puts all the files in a simple directory. This is simple but
  can be unreliable.
- ``chroot`` creates a chroot environment. This is more reliable as you get a
  harder isolation from the host system.
- ``installpkgs`` installs on your distribution the packages that were used by
  the experiment on the original machine. This is useful if some of them were
  not packed and you do not have them installed.
"""

from __future__ import unicode_literals

import argparse
import logging
import os
import pickle
import platform
from rpaths import PosixPath, DefaultAbstractPath, Path
import socket
import subprocess
import sys
import tarfile

from reprounzip.common import load_config as load_config_file, record_usage
from reprounzip import signals
from reprounzip.unpackers.common import THIS_DISTRIBUTION, PKG_NOT_INSTALLED, \
    COMPAT_OK, COMPAT_NO, UsageError, CantFindInstaller, target_must_exist, \
    shell_escape, load_config, select_installer, busybox_url, join_root, \
    FileUploader, FileDownloader, get_runs, interruptible_call
from reprounzip.unpackers.common.x11 import X11Handler, LocalForwarder
from reprounzip.utils import unicode_, irange, iteritems, itervalues, \
    make_dir_writable, rmtree_fixed, download_file


def installpkgs(args):
    """Installs the necessary packages on the current machine.
    """
    if not THIS_DISTRIBUTION:
        logging.critical("Not running on Linux")
        sys.exit(1)

    pack = args.pack[0]
    missing = args.missing

    # Loads config
    runs, packages, other_files = load_config(pack)

    try:
        installer = select_installer(pack, runs)
    except CantFindInstaller as e:
        logging.error("Couldn't select a package installer: %s", e)

    if args.summary:
        # Print out a list of packages with their status
        if missing:
            print("Packages not present in pack:")
            packages = [pkg for pkg in packages if not pkg.packfiles]
        else:
            print("All packages:")
        pkgs = installer.get_packages_info(packages)
        for pkg in packages:
            print("    %s (required version: %s, status: %s)" % (
                  pkg.name, pkg.version, pkgs[pkg.name][1]))
    else:
        if missing:
            # With --missing, ignore packages whose files were packed
            packages = [pkg for pkg in packages if not pkg.packfiles]

        # Installs packages
        record_usage(installpkgs_installing=len(packages))
        r, pkgs = installer.install(packages, assume_yes=args.assume_yes)
        for pkg in packages:
            req = pkg.version
            real = pkgs[pkg.name][1]
            if real == PKG_NOT_INSTALLED:
                logging.warning("package %s was not installed", pkg.name)
            else:
                logging.warning("version %s of %s was installed, instead of "
                                "%s", real, pkg.name, req)
        if r != 0:
            sys.exit(r)


def write_dict(filename, dct, type_):
    to_write = {'unpacker': type_}
    to_write.update(dct)
    with filename.open('wb') as fp:
        pickle.dump(to_write, fp, 2)


def read_dict(filename, type_):
    with filename.open('rb') as fp:
        dct = pickle.load(fp)
    if type_ is not None and dct['unpacker'] != type_:
        logging.critical("Wrong unpacker used: %s != %s" % (dct['unpacker'],
                                                            type_))
        raise UsageError
    return dct


def directory_create(args):
    """Unpacks the experiment in a folder.

    Only the files that are not part of a package are copied (unless they are
    missing from the system and were packed).

    In addition, input files are put in a tar.gz (so they can be put back after
    an upload) and the configuration file is extracted.
    """
    if not args.pack:
        logging.critical("setup needs the pack filename")
        sys.exit(1)

    pack = Path(args.pack[0])
    target = Path(args.target[0])
    if target.exists():
        logging.critical("Target directory exists")
        sys.exit(1)

    if not issubclass(DefaultAbstractPath, PosixPath):
        logging.critical("Not unpacking on POSIX system")
        sys.exit(1)

    signals.pre_setup(target=target, pack=pack)

    # Unpacks configuration file
    tar = tarfile.open(str(pack), 'r:*')
    member = tar.getmember('METADATA/config.yml')
    member.name = 'config.yml'
    tar.extract(member, str(target))

    # Loads config
    runs, packages, other_files = load_config_file(target / 'config.yml', True)

    target.mkdir()
    root = (target / 'root').absolute()
    root.mkdir()

    # Checks packages
    missing_files = False
    for pkg in packages:
        if pkg.packfiles:
            continue
        for f in pkg.files:
            f = Path(f.path)
            if not f.exists():
                logging.error(
                        "Missing file %s (from package %s that wasn't packed) "
                        "on host, experiment will probably miss it.",
                        f, pkg.name)
                missing_files = True
    if missing_files:
        record_usage(directory_missing_pkgs=True)
        logging.error(
                "Some packages are missing, you should probably install "
                "them.\nUse 'reprounzip installpkgs -h' for help")

    # Unpacks files
    if any('..' in m.name or m.name.startswith('/') for m in tar.getmembers()):
        logging.critical("Tar archive contains invalid pathnames")
        sys.exit(1)
    members = [m for m in tar.getmembers() if m.name.startswith('DATA/')]
    for m in members:
        m.name = m.name[5:]
    # Makes symlink targets relative
    for m in members:
        if not m.issym():
            continue
        linkname = PosixPath(m.linkname)
        if linkname.is_absolute:
            m.linkname = join_root(root, PosixPath(m.linkname)).path
    logging.info("Extracting files...")
    tar.extractall(str(root), members)
    tar.close()

    # Gets library paths
    lib_dirs = []
    p = subprocess.Popen(['/sbin/ldconfig', '-v', '-N'],
                         stdout=subprocess.PIPE)
    try:
        for l in p.stdout:
            if len(l) < 3 or l[0] in (b' ', b'\t'):
                continue
            if l.endswith(b':\n'):
                lib_dirs.append(Path(l[:-2]))
    finally:
        p.wait()

    # Original input files, so upload can restore them
    if any(run['input_files'] for run in runs):
        logging.info("Packing up original input files...")
        inputtar = tarfile.open(str(target / 'inputs.tar.gz'), 'w:gz')
        for run in runs:
            for ifile in itervalues(run['input_files']):
                inputtar.add(str(join_root(root, PosixPath(ifile))),
                             str(PosixPath(ifile)))
        inputtar.close()

    # Meta-data for reprounzip
    write_dict(target / '.reprounzip', {}, 'directory')

    signals.post_setup(target=target)


@target_must_exist
def directory_run(args):
    """Runs the command in the directory.
    """
    target = Path(args.target[0])
    read_dict(target / '.reprounzip', 'directory')
    cmdline = args.cmdline

    # Loads config
    runs, packages, other_files = load_config_file(target / 'config.yml', True)

    selected_runs = get_runs(runs, args.run, cmdline)

    root = (target / 'root').absolute()

    # Gets library paths
    lib_dirs = []
    p = subprocess.Popen(['/sbin/ldconfig', '-v', '-N'],
                         stdout=subprocess.PIPE)
    try:
        for l in p.stdout:
            if len(l) < 3 or l[0] in (b' ', b'\t'):
                continue
            if l.endswith(b':\n'):
                lib_dirs.append(Path(l[:-2]))
    finally:
        p.wait()
    lib_dirs = ('export LD_LIBRARY_PATH=%s' % ':'.join(
                shell_escape(unicode_(join_root(root, d)))
                for d in lib_dirs))

    cmds = [lib_dirs]
    for run_number in selected_runs:
        run = runs[run_number]
        cmd = 'cd %s && ' % shell_escape(
                unicode_(join_root(root,
                                   Path(run['workingdir']))))
        cmd += '/usr/bin/env -i '
        environ = run['environ']
        if args.x11:
            if 'DISPLAY' in os.environ:
                environ['DISPLAY'] = os.environ['DISPLAY']
            if 'XAUTHORITY' in os.environ:
                environ['XAUTHORITY'] = os.environ['XAUTHORITY']
        cmd += ' '.join('%s=%s' % (k, shell_escape(v))
                        for k, v in iteritems(environ)
                        if k != 'PATH')
        cmd += ' '

        # PATH
        # Get the original PATH components
        path = [PosixPath(d)
                for d in run['environ'].get('PATH', '').split(':')]
        # The same paths but in the directory
        dir_path = [join_root(root, d)
                    for d in path
                    if d.root == '/']
        # Rebuild string
        path = ':'.join(unicode_(d) for d in dir_path + path)
        cmd += 'PATH=%s ' % shell_escape(path)

        # FIXME : Use exec -a or something if binary != argv[0]
        if cmdline is None:
            argv = run['argv']

            # Rewrites command-line arguments that are absolute filenames
            rewritten = False
            for i in irange(len(argv)):
                try:
                    p = Path(argv[i])
                except UnicodeEncodeError:
                    continue
                if p.is_absolute:
                    rp = join_root(root, p)
                    if (rp.exists() or
                            (len(rp.components) > 3 and rp.parent.exists())):
                        argv[i] = str(rp)
                        rewritten = True
            if rewritten:
                logging.warning("Rewrote command-line as: %s",
                                ' '.join(shell_escape(a) for a in argv))
        else:
            argv = cmdline
        cmd += ' '.join(shell_escape(a) for a in argv)
        cmds.append(cmd)
    cmds = ' && '.join(cmds)

    signals.pre_run(target=target)
    retcode = interruptible_call(cmds, shell=True)
    sys.stderr.write("\n*** Command finished, status: %d\n" % retcode)
    signals.post_run(target=target, retcode=retcode)


@target_must_exist
def directory_destroy(args):
    """Destroys the directory.
    """
    target = Path(args.target[0])
    read_dict(target / '.reprounzip', 'directory')

    logging.info("Removing directory %s...", target)
    signals.pre_destroy(target=target)
    rmtree_fixed(target)
    signals.post_destroy(target=target)


def should_restore_owner(param):
    """Computes whether to restore original files' owners.
    """
    if os.getuid() != 0:
        if param is True:
            # Restoring the owner was explicitely requested
            logging.critical("Not running as root, cannot restore files' "
                             "owner/group as requested")
            sys.exit(1)
        elif param is None:
            # Nothing was requested
            logging.warning("Not running as root, won't restore files' "
                            "owner/group")
            ret = False
        else:
            # If False: skip warning
            ret = False
    else:
        if param is None:
            # Nothing was requested
            logging.info("Running as root, we will restore files' "
                         "owner/group")
            ret = True
        elif param is True:
            ret = True
        else:
            # If False: skip warning
            ret = False
    record_usage(restore_owner=ret)
    return ret


def should_mount_magic_dirs(param):
    """Computes whether to mount directories inside the chroot.
    """
    if os.getuid() != 0:
        if param is True:
            # Restoring the owner was explicitely requested
            logging.critical("Not running as root, cannot mount /dev and "
                             "/proc")
            sys.exit(1)
        elif param is None:
            # Nothing was requested
            logging.warning("Not running as root, won't mount /dev and /proc")
            ret = False
        else:
            # If False: skip warning
            ret = False
    else:
        if param is None:
            # Nothing was requested
            logging.info("Running as root, will mount /dev and /proc")
            ret = True
        elif param is True:
            ret = True
        else:
            # If False: skip warning
            ret = False
    record_usage(mount_magic_dirs=ret)
    return ret


def chroot_create(args):
    """Unpacks the experiment in a folder so it can be run with chroot.

    All the files in the pack are unpacked; system files are copied only if
    they were not packed, and busybox is installed if /bin/sh wasn't packed.

    In addition, input files are put in a tar.gz (so they can be put back after
    an upload) and the configuration file is extracted.
    """
    if not args.pack:
        logging.critical("setup/create needs the pack filename")
        sys.exit(1)

    pack = Path(args.pack[0])
    target = Path(args.target[0])
    if target.exists():
        logging.critical("Target directory exists")
        sys.exit(1)

    if DefaultAbstractPath is not PosixPath:
        logging.critical("Not unpacking on POSIX system")
        sys.exit(1)

    signals.pre_setup(target=target, pack=pack)

    # We can only restore owner/group of files if running as root
    restore_owner = should_restore_owner(args.restore_owner)

    # Unpacks configuration file
    tar = tarfile.open(str(pack), 'r:*')
    member = tar.getmember('METADATA/config.yml')
    member.name = 'config.yml'
    tar.extract(member, str(target))

    # Loads config
    runs, packages, other_files = load_config_file(target / 'config.yml', True)

    target.mkdir()
    root = (target / 'root').absolute()
    root.mkdir()

    # Checks that everything was packed
    packages_not_packed = [pkg for pkg in packages if not pkg.packfiles]
    if packages_not_packed:
        record_usage(chroot_missing_pkgs=True)
        logging.warning("According to configuration, some files were left out "
                        "because they belong to the following packages:%s"
                        "\nWill copy files from HOST SYSTEM",
                        ''.join('\n    %s' % pkg
                                for pkg in packages_not_packed))
        missing_files = False
        for pkg in packages_not_packed:
            for f in pkg.files:
                f = Path(f.path)
                if not f.exists():
                    logging.error(
                            "Missing file %s (from package %s) on host, "
                            "experiment will probably miss it",
                            f, pkg.name)
                    missing_files = True
                    continue
                dest = join_root(root, f)
                dest.parent.mkdir(parents=True)
                if f.is_link():
                    dest.symlink(f.read_link())
                else:
                    f.copy(dest)
                if restore_owner:
                    stat = f.stat()
                    dest.chown(stat.st_uid, stat.st_gid)
        if missing_files:
            record_usage(chroot_mising_files=True)

    # Unpacks files
    if any('..' in m.name or m.name.startswith('/') for m in tar.getmembers()):
        logging.critical("Tar archive contains invalid pathnames")
        sys.exit(1)
    members = [m for m in tar.getmembers() if m.name.startswith('DATA/')]
    for m in members:
        m.name = m.name[5:]
    if not restore_owner:
        uid = os.getuid()
        gid = os.getgid()
        for m in members:
            m.uid = uid
            m.gid = gid
    logging.info("Extracting files...")
    tar.extractall(str(root), members)
    tar.close()

    # Sets up /bin/sh and /usr/bin/env, downloading busybox if necessary
    sh_path = join_root(root, Path('/bin/sh'))
    env_path = join_root(root, Path('/usr/bin/env'))
    if not sh_path.lexists() or not env_path.lexists():
        logging.info("Setting up busybox...")
        busybox_path = join_root(root, Path('/bin/busybox'))
        busybox_path.parent.mkdir(parents=True)
        with make_dir_writable(join_root(root, Path('/bin'))):
            download_file(busybox_url(runs[0]['architecture']),
                          busybox_path)
            busybox_path.chmod(0o755)
            if not sh_path.lexists():
                sh_path.parent.mkdir(parents=True)
                sh_path.symlink('/bin/busybox')
            if not env_path.lexists():
                env_path.parent.mkdir(parents=True)
                env_path.symlink('/bin/busybox')

    # Original input files, so upload can restore them
    if any(run['input_files'] for run in runs):
        logging.info("Packing up original input files...")
        inputtar = tarfile.open(str(target / 'inputs.tar.gz'), 'w:gz')
        for run in runs:
            for ifile in itervalues(run['input_files']):
                inputtar.add(str(join_root(root, PosixPath(ifile))),
                             str(PosixPath(ifile)))
        inputtar.close()

    # Meta-data for reprounzip
    write_dict(target / '.reprounzip', {}, 'chroot')

    signals.post_setup(target=target)


@target_must_exist
def chroot_mount(args):
    """Mounts /dev and /proc inside the chroot directory.
    """
    target = Path(args.target[0])
    read_dict(target / '.reprounzip', 'chroot')

    for m in ('/dev', '/dev/pts', '/proc'):
        d = join_root(target / 'root', Path(m))
        d.mkdir(parents=True)
        logging.info("Mounting %s on %s...", m, d)
        subprocess.check_call(['mount', '-o', 'bind', m, str(d)])

    write_dict(target / '.reprounzip', {'mounted': True}, 'chroot')

    logging.warning("The host's /dev and /proc have been mounted into the "
                    "chroot. Do NOT remove the unpacked directory with "
                    "rm -rf, it WILL WIPE the host's /dev directory.")


@target_must_exist
def chroot_run(args):
    """Runs the command in the chroot.
    """
    target = Path(args.target[0])
    read_dict(target / '.reprounzip', 'chroot')
    cmdline = args.cmdline

    # Loads config
    runs, packages, other_files = load_config_file(target / 'config.yml', True)

    selected_runs = get_runs(runs, args.run, cmdline)

    root = target / 'root'

    # X11 handler
    x11 = X11Handler(args.x11, ('local', socket.gethostname()),
                     args.x11_display)

    cmds = []
    for run_number in selected_runs:
        run = runs[run_number]
        cmd = 'cd %s && ' % shell_escape(run['workingdir'])
        cmd += '/usr/bin/env -i '
        environ = x11.fix_env(run['environ'])
        cmd += ' '.join('%s=%s' % (k, shell_escape(v))
                        for k, v in iteritems(environ))
        cmd += ' '
        # FIXME : Use exec -a or something if binary != argv[0]
        if cmdline is None:
            argv = [run['binary']] + run['argv'][1:]
        else:
            argv = cmdline
        cmd += ' '.join(shell_escape(a) for a in argv)
        userspec = '%s:%s' % (run.get('uid', 1000),
                              run.get('gid', 1000))
        cmd = 'chroot --userspec=%s %s /bin/sh -c %s' % (
                userspec,
                shell_escape(unicode_(root)),
                shell_escape(cmd))
        cmds.append(cmd)
    cmds = ['chroot %s /bin/sh -c %s' % (shell_escape(unicode_(root)),
                                         shell_escape(c))
            for c in x11.init_cmds] + cmds
    cmds = ' && '.join(cmds)

    # Starts forwarding
    forwarders = []
    for portnum, connector in x11.port_forward:
        fwd = LocalForwarder(connector, portnum)
        forwarders.append(fwd)

    signals.pre_run(target=target)
    retcode = interruptible_call(cmds, shell=True)
    sys.stderr.write("\n*** Command finished, status: %d\n" % retcode)
    signals.post_run(target=target, retcode=retcode)


def chroot_unmount(target):
    """Unmount magic directories, if they are mounted.
    """
    unpacked_info = read_dict(target / '.reprounzip', 'chroot')
    mounted = unpacked_info.get('mounted', False)

    if not mounted:
        return False

    target = target.resolve()
    for m in ('/dev', '/proc'):
        d = join_root(target / 'root', Path(m))
        if d.exists():
            logging.info("Unmounting %s...", d)
            # Unmounts recursively
            subprocess.check_call(
                    'grep %s /proc/mounts | '
                    'cut -f2 -d" " | '
                    'sort -r | '
                    'xargs umount' % d,
                    shell=True)

    unpacked_info['mounted'] = False
    write_dict(target / '.reprounzip', unpacked_info, 'chroot')

    return True


@target_must_exist
def chroot_destroy_unmount(args):
    """Unmounts the bound magic dirs.
    """
    target = Path(args.target[0])

    if not chroot_unmount(target):
        logging.critical("Magic directories were not mounted")
        sys.exit(1)


@target_must_exist
def chroot_destroy_dir(args):
    """Destroys the directory.
    """
    target = Path(args.target[0])
    mounted = read_dict(target / '.reprounzip', 'chroot').get('mounted', False)

    if mounted:
        logging.critical("Magic directories might still be mounted")
        sys.exit(1)

    logging.info("Removing directory %s...", target)
    signals.pre_destroy(target=target)
    rmtree_fixed(target)
    signals.post_destroy(target=target)


@target_must_exist
def chroot_destroy(args):
    """Destroys the directory, unmounting first if necessary.
    """
    target = Path(args.target[0])

    chroot_unmount(target)

    logging.info("Removing directory %s...", target)
    signals.pre_destroy(target=target)
    rmtree_fixed(target)
    signals.post_destroy(target=target)


class LocalUploader(FileUploader):
    def __init__(self, target, input_files, files, type_, param_restore_owner):
        self.type = type_
        self.param_restore_owner = param_restore_owner
        FileUploader.__init__(self, target, input_files, files)

    def prepare_upload(self, files):
        self.restore_owner = (self.type == 'chroot' and
                              should_restore_owner(self.param_restore_owner))
        self.root = (self.target / 'root').absolute()

    def extract_original_input(self, input_name, input_path, temp):
        tar = tarfile.open(str(self.target / 'inputs.tar.gz'), 'r:*')
        member = tar.getmember(str(join_root(PosixPath(''), input_path)))
        member.name = str(temp.name)
        tar.extract(member, str(temp.parent))
        tar.close()
        return temp

    def upload_file(self, local_path, input_path):
        remote_path = join_root(self.root, input_path)

        # Copy
        orig_stat = remote_path.stat()
        with make_dir_writable(remote_path.parent):
            local_path.copyfile(remote_path)
            remote_path.chmod(orig_stat.st_mode & 0o7777)
            if self.restore_owner:
                remote_path.chown(orig_stat.st_uid, orig_stat.st_gid)


@target_must_exist
def upload(args):
    """Replaces an input file in the directory.
    """
    target = Path(args.target[0])
    files = args.file
    unpacked_info = read_dict(target / '.reprounzip', args.type)
    input_files = unpacked_info.setdefault('input_files', {})

    try:
        LocalUploader(target, input_files, files,
                      args.type, args.type == 'chroot' and args.restore_owner)
    finally:
        write_dict(target / '.reprounzip', unpacked_info, args.type)


class LocalDownloader(FileDownloader):
    def __init__(self, target, files, type_):
        self.type = type_
        FileDownloader.__init__(self, target, files)

    def prepare_download(self, files):
        self.root = (self.target / 'root').absolute()

    def download_and_print(self, remote_path):
        remote_path = join_root(self.root, remote_path)

        # Output to stdout
        if not remote_path.exists():
            logging.critical("Can't get output file (doesn't exist): %s",
                             remote_path)
            sys.exit(1)
        with remote_path.open('rb') as fp:
            chunk = fp.read(1024)
            if chunk:
                sys.stdout.buffer.write(chunk)
            while len(chunk) == 1024:
                chunk = fp.read(1024)
                if chunk:
                    sys.stdout.buffer.write(chunk)

    def download(self, remote_path, local_path):
        remote_path = join_root(self.root, remote_path)

        # Copy
        if not remote_path.exists():
            logging.critical("Can't get output file (doesn't exist): %s",
                             remote_path)
            sys.exit(1)
        remote_path.copyfile(local_path)
        remote_path.copymode(local_path)


@target_must_exist
def download(args):
    """Gets an output file from the directory.
    """
    target = Path(args.target[0])
    files = args.file
    read_dict(target / '.reprounzip', args.type)

    LocalDownloader(target, files, args.type)


def test_same_pkgmngr(pack, config, **kwargs):
    """Compatibility test: platform is Linux and uses same package manager.
    """
    runs, packages, other_files = config

    orig_distribution = runs[0]['distribution'][0].lower()
    if not THIS_DISTRIBUTION:
        return COMPAT_NO, "This machine is not running Linux"
    elif THIS_DISTRIBUTION == orig_distribution:
        return COMPAT_OK
    else:
        return COMPAT_NO, "Different distributions. Then: %s, now: %s" % (
                orig_distribution, THIS_DISTRIBUTION)


def test_linux_same_arch(pack, config, **kwargs):
    """Compatibility test: this platform is Linux and arch is compatible.
    """
    runs, packages, other_files = config

    orig_architecture = runs[0]['architecture']
    current_architecture = platform.machine().lower()
    if platform.system().lower() != 'linux':
        return COMPAT_NO, "This machine is not running Linux"
    elif (orig_architecture == current_architecture or
            (orig_architecture == 'i386' and current_architecture == 'amd64')):
        return COMPAT_OK
    else:
        return COMPAT_NO, "Different architectures. Then: %s, now: %s" % (
                orig_architecture, current_architecture)


def setup_installpkgs(parser):
    """Installs the required packages on this system
    """
    parser.add_argument('pack', nargs=1, help="Pack to process")
    parser.add_argument(
            '-y', '--assume-yes', action='store_true', default=False,
            help="Assumes yes for package manager's questions (if supported)")
    parser.add_argument(
            '--missing', action='store_true',
            help="Only install packages that weren't packed")
    parser.add_argument(
            '--summary', action='store_true',
            help="Don't install, print which packages are installed or not")
    parser.set_defaults(func=installpkgs)

    return {'test_compatibility': test_same_pkgmngr}


def setup_directory(parser, **kwargs):
    """Unpacks the files in a directory and runs with PATH and LD_LIBRARY_PATH

    setup       creates the directory (needs the pack filename)
    upload      replaces input files in the directory
                (without arguments, lists input files)
    run         runs the experiment
    download    gets output files
                (without arguments, lists output files)
    destroy     removes the unpacked directory

    Upload specifications are either:
      :input_id             restores the original input file from the pack
      filename:input_id     replaces the input file with the specified local
                            file

    Download specifications are either:
      output_id:            print the output file to stdout
      output_id:filename    extracts the output file to the corresponding local
                            path
    """
    subparsers = parser.add_subparsers(title="actions",
                                       metavar='', help=argparse.SUPPRESS)
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument('target', nargs=1, help="Experiment directory")

    # setup
    # Note: opt_setup is a separate parser so that 'pack' is before 'target'
    opt_setup = argparse.ArgumentParser(add_help=False)
    opt_setup.add_argument('pack', nargs=1, help="Pack to extract")
    parser_setup = subparsers.add_parser('setup', parents=[opt_setup, options])
    parser_setup.set_defaults(func=directory_create)

    # upload
    parser_upload = subparsers.add_parser('upload', parents=[options])
    parser_upload.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                               help="<path>:<input_file_name>")
    parser_upload.set_defaults(func=upload, type='directory')

    # run
    parser_run = subparsers.add_parser('run', parents=[options])
    parser_run.add_argument('run', default=None, nargs='?')
    parser_run.add_argument('--cmdline', nargs=argparse.REMAINDER,
                            help="Command line to run")
    parser_run.add_argument('--enable-x11', action='store_true', default=False,
                            dest='x11',
                            help="Enable X11 support (needs an X server)")
    parser_run.set_defaults(func=directory_run)

    # download
    parser_download = subparsers.add_parser('download',
                                            parents=[options])
    parser_download.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                                 help="<output_file_name>:<path>")
    parser_download.set_defaults(func=download, type='directory')

    # destroy
    parser_destroy = subparsers.add_parser('destroy', parents=[options])
    parser_destroy.set_defaults(func=directory_destroy)

    return {'test_compatibility': test_linux_same_arch}


def chroot_setup(args):
    """Does both create and mount depending on --bind-magic-dirs.
    """
    chroot_create(args)
    if should_mount_magic_dirs(args.bind_magic_dirs):
        chroot_mount(args)


def setup_chroot(parser, **kwargs):
    """Unpacks the files and run with chroot

    setup/create    creates the directory (needs the pack filename)
    setup/mount     mounts --bind /dev and /proc inside the chroot
                    (do NOT rm -Rf the directory after that!)
    upload          replaces input files in the directory
                    (without arguments, lists input files)
    run             runs the experiment
    download        gets output files
                    (without arguments, lists output files)
    destroy/unmount unmounts /dev and /proc from the directory
    destroy/dir     removes the unpacked directory

    Upload specifications are either:
      :input_id             restores the original input file from the pack
      filename:input_id     replaces the input file with the specified local
                            file

    Download specifications are either:
      output_id:            print the output file to stdout
      output_id:filename    extracts the output file to the corresponding local
                            path
    """
    subparsers = parser.add_subparsers(title="actions",
                                       metavar='', help=argparse.SUPPRESS)
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument('target', nargs=1, help="Experiment directory")

    # setup/create
    opt_setup = argparse.ArgumentParser(add_help=False)
    opt_setup.add_argument('pack', nargs=1, help="Pack to extract")
    opt_owner = argparse.ArgumentParser(add_help=False)
    opt_owner.add_argument('--preserve-owner', action='store_true',
                           dest='restore_owner', default=None,
                           help="Restore files' owner/group when extracting")
    opt_owner.add_argument('--dont-preserve-owner', action='store_false',
                           dest='restore_owner', default=None,
                           help=("Don't restore files' owner/group when "
                                 "extracting, use current users"))
    parser_setup_create = subparsers.add_parser(
            'setup/create',
            parents=[opt_setup, options, opt_owner])
    parser_setup_create.set_defaults(func=chroot_create)

    # setup/mount
    parser_setup_mount = subparsers.add_parser('setup/mount',
                                               parents=[options])
    parser_setup_mount.set_defaults(func=chroot_mount)

    # setup
    parser_setup = subparsers.add_parser(
            'setup',
            parents=[opt_setup, options, opt_owner])
    parser_setup.add_argument(
            '--bind-magic-dirs', action='store_true',
            dest='bind_magic_dirs', default=None,
            help="Mount /dev and /proc inside the chroot")
    parser_setup.add_argument(
            '--dont-bind-magic-dirs', action='store_false',
            dest='bind_magic_dirs', default=None,
            help="Don't mount /dev and /proc inside the chroot")
    parser_setup.set_defaults(func=chroot_setup)

    # upload
    parser_upload = subparsers.add_parser('upload',
                                          parents=[options, opt_owner])
    parser_upload.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                               help="<path>:<input_file_name>")
    parser_upload.set_defaults(func=upload, type='chroot')

    # run
    parser_run = subparsers.add_parser('run', parents=[options])
    parser_run.add_argument('run', default=None, nargs='?')
    parser_run.add_argument('--cmdline', nargs=argparse.REMAINDER,
                            help="Command line to run")
    parser_run.add_argument('--enable-x11', action='store_true', default=False,
                            dest='x11',
                            help=("Enable X11 support (needs an X server on "
                                  "the host)"))
    parser_run.add_argument('--x11-display', dest='x11_display',
                            help=("Display number to use on the experiment "
                                  "side (change the host display with the "
                                  "DISPLAY environment variable)"))
    parser_run.set_defaults(func=chroot_run)

    # download
    parser_download = subparsers.add_parser('download',
                                            parents=[options])
    parser_download.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                                 help="<output_file_name>:<path>")
    parser_download.set_defaults(func=download, type='chroot')

    # destroy/unmount
    parser_destroy_unmount = subparsers.add_parser('destroy/unmount',
                                                   parents=[options])
    parser_destroy_unmount.set_defaults(func=chroot_destroy_unmount)

    # destroy/dir
    parser_destroy_dir = subparsers.add_parser('destroy/dir',
                                               parents=[options])
    parser_destroy_dir.set_defaults(func=chroot_destroy_dir)

    # destroy
    parser_destroy = subparsers.add_parser('destroy', parents=[options])
    parser_destroy.set_defaults(func=chroot_destroy)

    return {'test_compatibility': test_linux_same_arch}
