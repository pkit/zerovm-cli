import ConfigParser
import argparse
import os
import shutil
import stat
from subprocess import Popen, PIPE
import sys
import tarfile
from tempfile import mkdtemp
import threading
import re

ENV_MATCH = re.compile(r'([_A-Z0-9]+)=(.*)')
DEFAULT_MANIFEST = {
    'Version': '20130611',
    'Memory': '%d, 0' % (4 * 1024 * 1024 * 1024),
    'Node': 1,
    'Timeout': 50
}
DEFAULT_LIMITS = {
    'reads': str(1024 * 1024 * 1024 * 4),
    'rbytes': str(1024 * 1024 * 1024 * 4),
    'writes': str(1024 * 1024 * 1024 * 4),
    'wbytes': str(1024 * 1024 * 1024 * 4)
}
CHANNEL_SEQ_READ_TEMPLATE = 'Channel = %s,%s,0,0,%s,%s,0,0'
CHANNEL_SEQ_WRITE_TEMPLATE = 'Channel = %s,%s,0,0,0,0,%s,%s'
CHANNEL_RANDOM_RW_TEMPLATE = 'Channel = %s,%s,3,0,%s,%s,%s,%s'
CHANNEL_RANDOM_RO_TEMPLATE = 'Channel = %s,%s,3,0,%s,%s,0,0'

DEBUG_TEMPLATE = '''set confirm off
b CreateSession
r
b main
add-symbol-file %s 0x440a00020000
shell clear
c
d br
'''


class ZvArgs:

    def __init__(self):
        self.parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
        self.parser.add_argument('command', help='Zvsh command, can be:\n'
                                                 '- path to ZeroVM executable\n'
                                                 '- "gdb" (for running debugger)\n')
        self.parser.add_argument('--zvm-image', help='ZeroVM image file(s) in the following '
                                                     'format:\npath[,mount point][,access type]\n'
                                                     'defaults: path,/,ro\n', action='append')
        self.parser.add_argument('--zvm-debug', help='Enable ZeroVM debug output into zvsh.log\n',
                                 action='store_true')
        self.parser.add_argument('--zvm-trace', help='Enable ZeroVM trace output into zvsh.trace.log\n',
                                 action='store_true')
        self.parser.add_argument('--zvm-verbosity', help='ZeroVM debug verbosity level', type=int)
        self.parser.add_argument('--zvm-save-dir', help='Save ZeroVM environment files into provided directory,\n'
                                                        'directory will be created/re-created\n',
                                 action='store')
        self.parser.add_argument('cmd_args', help='command line arguments\n', nargs=argparse.REMAINDER)
        self.args = None

    def parse(self, zvsh_args):
        self.args = self.parser.parse_args(args=zvsh_args)


class DebugArgs(ZvArgs):

    def parse(self, zvsh_args):
        self.args = self.parser.parse_args(args=zvsh_args)
        self.args.gdb_args = []
        while self.args.cmd_args:
            arg = self.args.cmd_args.pop(0)
            if arg == '--args':
                break
            self.args.gdb_args.append(arg)
        self.args.command = self.args.cmd_args.pop(0)


class ZvShell:

    def __init__(self, config_files, use_fifo=True,
                 stdin=None, stdout=None, stderr=None):
        self.temp_files = []
        self.nvram_env = {}
        self.nvram_fstab = {}
        self.nvram_args = None
        self.nvram_filename = None
        self.program = None
        self.manifest_conf = DEFAULT_MANIFEST
        self.channel_conf = DEFAULT_LIMITS
        config = ConfigParser.ConfigParser()
        config.optionxform = str
        config.read(config_files)
        try:
            self.manifest_conf.update(dict(config.items('manifest')))
        except ConfigParser.NoSectionError:
            pass
        try:
            self.nvram_env.update(dict(config.items('env')))
        except ConfigParser.NoSectionError:
            pass
        try:
            self.channel_conf.update(dict(config.items('limits')))
        except ConfigParser.NoSectionError:
            pass
        self.node_id = self.manifest_conf['Node']
        self.savedir = None
        self.tmpdir = mkdtemp()
        if stdout:
            self.stdout = stdout
        else:
            self.stdout = os.path.join(self.tmpdir, 'stdout.%d' % self.node_id)
            if use_fifo:
                os.mkfifo(self.stdout)
        if stderr:
            self.stderr = stderr
        else:
            self.stderr = os.path.join(self.tmpdir, 'stderr.%d' % self.node_id)
            if use_fifo:
                os.mkfifo(self.stderr)
        if not stdin:
            stdin = '/dev/stdin'
        self.manifest_channels = [
            CHANNEL_SEQ_READ_TEMPLATE
            % (stdin, '/dev/stdin', self.channel_conf['reads'], self.channel_conf['rbytes']),
            CHANNEL_SEQ_WRITE_TEMPLATE
            % (self.stdout, '/dev/stdout', self.channel_conf['writes'], self.channel_conf['wbytes']),
            CHANNEL_SEQ_WRITE_TEMPLATE
            % (self.stderr, '/dev/stderr', self.channel_conf['writes'], self.channel_conf['wbytes'])
        ]
        try:
            for k, v in dict(config.items('fstab')).iteritems():
                self.nvram_fstab[self.create_manifest_channel(k)] = v
        except ConfigParser.NoSectionError:
            pass

    def create_manifest_channel(self, file_name):
        name = os.path.basename(file_name)
        self.temp_files.append(file_name)
        devname = '/dev/%s.%s' % (len(self.temp_files), name)
        if os.access(os.path.abspath(file_name), os.W_OK):
            self.manifest_channels.append(CHANNEL_RANDOM_RW_TEMPLATE
                                          % (os.path.abspath(file_name), devname,
                                             self.channel_conf['reads'], self.channel_conf['rbytes'],
                                             self.channel_conf['writes'], self.channel_conf['wbytes']))
        else:
            self.manifest_channels.append(CHANNEL_RANDOM_RO_TEMPLATE
                                          % (os.path.abspath(file_name), devname,
                                             self.channel_conf['reads'], self.channel_conf['rbytes']))
        return devname

    def add_untrusted_args(self, program, cmdline):
        self.program = program
        untrusted_args = [os.path.basename(program)]
        for arg in cmdline:
            if arg.startswith('@'):
                arg = arg[1:]
                m = ENV_MATCH.match(arg)
                if m:
                    self.nvram_env[m.group(1)] = m.group(2)
                else:
                    dev_name = self.create_manifest_channel(arg)
                    untrusted_args.append(dev_name)
            else:
                untrusted_args.append(arg)

        self.nvram_args = {
            'args': untrusted_args
        }

    def add_image_args(self, zvm_image):
        if not zvm_image:
            return
        for img in zvm_image:
            (imgpath, imgmp, imgacc) = (img.split(',') + [None] * 3)[:3]
            dev_name = self.create_manifest_channel(imgpath)
            self.nvram_fstab[dev_name] = '%s %s' % (imgmp or '/', imgacc or 'ro')
            tar = tarfile.open(name=imgpath)
            nexe = None
            try:
                nexe = tar.extractfile(self.program)
                tmpnexe_fn = os.path.join(self.tmpdir, 'boot.%d' % self.node_id)
                tmpnexe_fd = open(tmpnexe_fn, 'wb')
                read_iter = iter(lambda: nexe.read(65535), '')
                for chunk in read_iter:
                    tmpnexe_fd.write(chunk)
                tmpnexe_fd.close()
                self.program = tmpnexe_fn
            except KeyError:
                pass

    def add_debug(self, zvm_debug):
        if zvm_debug:
            self.manifest_channels.append(CHANNEL_SEQ_WRITE_TEMPLATE
                                          % (os.path.abspath('zvsh.log'), '/dev/debug',
                                             self.channel_conf['writes'], self.channel_conf['wbytes']))

    def create_nvram(self, verbosity):
        nvram = '[args]\n'
        nvram += 'args = %s\n' % ' '.join([a.replace(',', '\\x2c') for a in self.nvram_args['args']])
        if len(self.nvram_env) > 0:
            nvram += '[env]\n'
            for k, v in self.nvram_env.iteritems():
                nvram += 'name=%s,value=%s\n' % (k, v.replace(',', '\\x2c'))
        if len(self.nvram_fstab) > 0:
            nvram += '[fstab]\n'
            for channel, mount in self.nvram_fstab.iteritems():
                (mp, access) = mount.split()
                nvram += 'channel=%s,mountpoint=%s,access=%s,removable=no\n' % (channel, mp, access)
        if sys.stdin.isatty() or sys.stdout.isatty() or sys.stderr.isatty():
            nvram += '[mapping]\n'
            if sys.stdin.isatty():
                nvram += 'channel=/dev/stdin,mode=char\n'
            if sys.stdout.isatty():
                nvram += 'channel=/dev/stdout,mode=char\n'
            if sys.stderr.isatty():
                nvram += 'channel=/dev/stderr,mode=char\n'
        if verbosity:
            nvram += '[debug]\nverbosity=%d\n' % verbosity
        self.nvram_filename = os.path.join(self.tmpdir, 'nvram.%d' % self.node_id)
        nvram_fd = open(self.nvram_filename, 'wb')
        nvram_fd.write(nvram)
        nvram_fd.close()

    def create_manifest(self):
        manifest = ''
        for k, v in self.manifest_conf.iteritems():
            manifest += '%s = %s\n' % (k, v)
        manifest += 'Program = %s\n' % os.path.abspath(self.program)
        self.manifest_channels.append(CHANNEL_RANDOM_RW_TEMPLATE
                                      % (os.path.abspath(self.nvram_filename), '/dev/nvram',
                                         self.channel_conf['reads'], self.channel_conf['rbytes'],
                                         self.channel_conf['writes'], self.channel_conf['wbytes']))
        manifest += '\n'.join(self.manifest_channels)
        manifest_fn = os.path.join(self.tmpdir, 'manifest.%d' % self.node_id)
        manifest_fd = open(manifest_fn, 'wb')
        manifest_fd.write(manifest)
        manifest_fd.close()
        return manifest_fn

    def add_arguments(self, args):
        self.add_debug(args.zvm_debug)
        self.add_untrusted_args(args.command, args.cmd_args)
        self.add_image_args(args.zvm_image)
        self.create_nvram(args.zvm_verbosity)
        manifest_file = self.create_manifest()
        self.savedir = args.zvm_save_dir
        return manifest_file

    def cleanup(self):
        if self.savedir:
            try:
                if os.path.exists(self.savedir):
                    shutil.rmtree(self.savedir)
                os.rename(self.tmpdir, self.savedir)
            except OSError, e:
                sys.stderr.write(str(e) + '\n')
                shutil.rmtree(self.tmpdir, ignore_errors=True)
        else:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_debug_script(self):
        exec_path = os.path.abspath(self.program)
        debug_scp = DEBUG_TEMPLATE % exec_path
        debug_scp_fn = os.path.join(self.tmpdir, 'debug.scp')
        debug_scp_fd = open(debug_scp_fn, 'wb')
        debug_scp_fd.write(debug_scp)
        debug_scp_fd.close()
        return debug_scp_fn


class ZvRunner:

    def __init__(self, command_line, stdout, stderr, tempdir):
        self.command = command_line
        self.tmpdir = tempdir
        self.process = None
        self.stdout = stdout
        self.stderr = stderr
        self.report = ''

    def run(self):
        try:
            self.process = Popen(self.command, stdin=PIPE, stdout=PIPE)
            self.spawn(True, self.stdin_reader)
            err_reader = self.spawn(True, self.stderr_reader)
            rep_reader = self.spawn(True, self.report_reader)
            writer = self.spawn(True, self.stdout_write)
            self.process.wait()
            rep_reader.join()
            if self.process.returncode == 0:
                writer.join()
                err_reader.join()
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            if self.process:
                self.process.wait()
                if self.process.returncode > 0:
                    self.print_error(self.process.returncode)

    def stdin_reader(self):
        if sys.stdin.isatty():
            try:
                for l in sys.stdin:
                    self.process.stdin.write(l)
            except IOError:
                pass
        else:
            try:
                for l in iter(lambda: sys.stdin.read(65535), ''):
                    self.process.stdin.write(l)
            except IOError:
                pass
        self.process.stdin.close()

    def stderr_reader(self):
        err = open(self.stderr)
        try:
            for l in iter(lambda: err.read(65535), ''):
                sys.stderr.write(l)
        except IOError:
            pass
        err.close()

    def stdout_write(self):
        pipe = open(self.stdout)
        if sys.stdout.isatty():
            for line in pipe:
                sys.stdout.write(line)
        else:
            for line in iter(lambda: pipe.read(65535), ''):
                sys.stdout.write(line)
        pipe.close()

    def report_reader(self):
        for line in iter(lambda: self.process.stdout.read(65535), ''):
            self.report += line

    def spawn(self, daemon, func, **kwargs):
        thread = threading.Thread(target=func, kwargs=kwargs)
        thread.daemon = daemon
        thread.start()
        return thread

    def print_error(self, rc):
        for f in os.listdir(self.tmpdir):
            path = os.path.join(self.tmpdir, f)
            if stat.S_ISREG(os.stat(path).st_mode):
                if is_binary_string(open(path).read(1024)):
                    sys.stderr.write('%s is a binary file\n' % path)
                else:
                    sys.stderr.write('\n'.join(['-' * 10 + f + '-' * 10, open(path).read(), '-' * 25, '']))
        sys.stderr.write(self.report)
        sys.stderr.write("ERROR: ZeroVM return code is %d\n" % rc)


def is_binary_string(byte_string):
    textchars = ''.join(map(chr, [7, 8, 9, 10, 12, 13, 27] + range(0x20, 0x100)))
    return bool(byte_string.translate(None, textchars))