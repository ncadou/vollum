#!/usr/bin/env python

import os
import re
import sys
from os.path import basename, expanduser, join, lexists, sep
from subprocess import CalledProcessError, call, check_output
from tempfile import mkstemp

import click
import yaml
from bunch import Bunch, bunchify
from pyudev import Context, Monitor

ACTIONS = dict(add='on_add', remove='on_remove')

click.disable_unicode_literals_warning = True


MOUNTS_RE = re.compile(
    r'^([^\s]+)\s+on\s+([^\s]+)\s+type\s+([^\s]+)\s+\((.+)\)$')

deps = bunchify(dict(parents=dict(), children=dict()))
settings = None

# Needed system packages (Ubuntu):
# - cryptsetup
# - pmount
# - python-pip
# - python-virtualenv


@click.group()
@click.option('-c', '--config', envvar='CONFIG',
              help='Name of the configuration file to use.')
@click.pass_context
def cli(ctx, config='config.yml'):
    """Manipulate storage devices."""
    global settings
    with open(config) as config:
        settings = bunchify(yaml.load(config.read()))
    defaults = settings.defaults
    for name, conf in settings.devices.iteritems():
        if name.startswith('_'):
            continue

        if 'parent' in conf:
            deps.children[conf.parent] = name
            deps.parents[name] = conf.parent
        if 'auto_mount' in conf and 'symlink' not in conf:
            conf.symlink = join(defaults.base_link_dir, name)
        if 'key' in conf:
            conf.password_manager = defaults.password_manager
    uuids = dict((conf.uuid, name)
                 for name, conf in settings.devices.iteritems()
                 if 'uuid' in conf)
    ctx.obj.update(uuids=uuids)


@cli.command('mount')
@click.argument('name')
@click.pass_context
def cli_mount(ctx, name):
    """Mount device filesystem."""
    conf, devname, label = find(ctx, name)
    if name in deps.parents:
        ctx.invoke(cli_mount, name=deps.parents[name])
    if not get_mount_info(devname, label):
        PMount(conf, name, devname, label=label).mount()


@cli.command('umount')
@click.argument('name')
@click.pass_context
def cli_umount(ctx, name):
    """Unmount device filesystem."""
    conf, devname, label = find(ctx, name)
    if name in deps.children:
        ctx.invoke(cli_umount, name=deps.children[name])
    info = get_mount_info(devname, label)
    if info:
        PMount(conf, name, info.device, label=label).umount()


@cli.command('watch')
@click.pass_context
def cli_watch(ctx):
    """Handle device plug events."""
    uuids = ctx.obj['uuids']

    def handler(dev):
        name = uuids.get(dev.get('ID_FS_UUID'))
        conf = settings.devices.get(name, dict())
        devname = dev['DEVNAME']
        label = conf.get('label', dev.get('ID_FS_LABEL'))
        print('Block device %s %s (name=%s, label=%s, uuid=%s)%s' %
              (dev.action, devname, name, label, dev.get('ID_FS_UUID'),
               ' (nop)' if not conf else ''))
        if not conf:
            return

        command = conf.get(ACTIONS.get(dev.action))
        if command:
            print('Running command: %s' % command)
            call(command, shell=True)
        if dev.action == 'add' and conf.get('auto_mount'):
            PMount(conf, name, devname, label=label).mount(error='ignore')
        if dev.action == 'remove':
            info = get_mount_info(devname, label)
            if info:
                PMount(conf, name, info.device, label=label).umount(
                    error='ignore')

    poll(handler)


def poll(callback):
    """Invoke callback upon udev activity."""
    context = Context()
    monitor = Monitor.from_netlink(context)
    monitor.filter_by(subsystem='block')
    for dev in iter(monitor.poll, None):
        if 'ID_FS_TYPE' in dev:
            callback(dev)


def find(ctx, name):
    """Find device by name."""
    conf = settings.devices.get(name, dict())
    if conf.get('type') == 'command':
        return conf, name, name

    uuids = ctx.obj['uuids']
    context = Context()
    for dev in iter(context.list_devices()):
        if 'ID_FS_TYPE' in dev:
            if name == uuids.get(dev.get('ID_FS_UUID')):
                return (settings.devices[name], dev['DEVNAME'],
                        settings.devices[name].get('label',
                                                   dev.get('ID_FS_LABEL')))

    print('Device "%s" not found.' % name)
    sys.exit(1)


class Volume(object):
    def __init__(self, conf, name, devname, label=None, **kw):
        self.conf = conf
        self.name = name
        self.devname = devname
        self.label = label
        self.kw = kw

    def mount(self):
        pass

    def umount(self):
        pass


class PMount(Volume):
    def mount(self, *args, **kw):
        """Mount device filesystem."""
        args = (('-t', self.conf.get('type', 'vfat')) +
                (self.conf.get('sync', ()) and ('--sync',)) + args)
        if 'key' in self.conf:
            filehandle, tmpfile = mkstemp()
            password_command = ' '.join([self.conf.password_manager,
                                         self.conf.key])
            try:
                passwd = check_output(password_command, shell=True).strip()
            except CalledProcessError as e:
                print('Unable to get password for device %s.' % self.devname)
                if kw.get('error') == 'ignore':
                    return

                else:
                    sys.exit(e.returncode)

            os.write(filehandle, passwd)
            os.close(filehandle)
            args += ('-p', tmpfile)
        env = dict(MOUNT_POINT=get_mount_target(self.devname, self.label))
        env.update(self.conf.get('env', dict()))
        if 'mount_cmd' in self.conf:
            result = call_cmd(self.name, self.conf.mount_cmd, env=env)
            if result and kw.get('error') == 'exit':
                exit(result)

        else:
            self._pmount('mount', args, **kw)
        if 'key' in self.conf:
            os.unlink(tmpfile)
        if 'post_mount_cmd' in self.conf:
            result = call_cmd(self.name, self.conf.post_mount_cmd, env=env)
            if result and kw.get('error') == 'exit':
                exit(result)

        _symlink(self.conf, self.devname, self.label)

    def umount(self, *args, **kw):
        """Unmount device filesystem."""
        if 'umount_cmd' in self.conf:
            result = call_cmd(self.name, self.conf.umount_cmd)
            if result and kw.get('error') == 'exit':
                exit(result)

        else:
            self._pmount('umount', args, **kw)
        _symlink(self.conf, self.devname, self.label, remove=True)

    def _pmount(self, action, args, error='exit'):
        """Run pmount on device filesystem."""
        args = ('p%s' % action,) + args + (self.devname,)
        if action == 'mount':
            if self.label:
                args += (self.label,)
            msg = 'Mounting %s on %s'
        else:
            msg = 'Unmounting %s from %s'
        print(msg % (self.devname, get_mount_target(self.devname, self.label)))
        result = call(args)
        if result and error == 'exit':
            exit(result)


def call_cmd(name, command, env=None, **vars):
    """Unmount device filesystem."""
    vars = dict(name=name, **vars)
    for _name, _conf in settings.devices.iteritems():
        if 'label' in _conf:
            vars[_name] = get_mount_target(devname=None, label=_conf.label)
    command = command.format(**vars)
    if env is not None:
        for k, v in env.iteritems():
            env[k] = expanduser(v)
        env = dict(os.environ.items() + env.items())
    return call(command, env=env, shell=True)


def get_mount_target(devname, label=None):
    """Return the mount point that pmount will use."""
    return join(sep, 'media', label or basename(devname))


def get_mount_info(devname, label=None):
    """Return True if device is mounted."""
    mount_point = get_mount_target(devname, label)
    mounts = check_output('mount | grep " %s " || :' % mount_point, shell=True)
    if mounts:
        return Bunch(zip(('device', 'mount_point', 'type', 'options'),
                         MOUNTS_RE.match(mounts).groups()))


def _symlink(conf, devname, label, remove=False):
    """Create a symlink, cleaning up first."""
    return

    linkpath = conf.get('symlink')
    if linkpath:
        linkpath = expanduser(linkpath)
        if lexists(linkpath):
            os.unlink(linkpath)
        if not remove:
            # TODO: handle path errors
            os.symlink(get_mount_target(devname, label), linkpath)


if __name__ == '__main__':
    cli(obj=dict())
