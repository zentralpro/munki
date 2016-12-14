#!/usr/bin/python
# encoding: utf-8
#
# Copyright 2009-2016 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
info.py

Created by Greg Neagle on 2016-12-14.

Utilities that retrieve information from the current machine.
"""
import ctypes
import ctypes.util
import fcntl
import os
import select
import struct
import subprocess
import sys

import LaunchServices
# PyLint cannot properly find names inside Cocoa libraries, so issues bogus
# No name 'Foo' in module 'Bar' warnings. Disable them.
# pylint: disable=E0611
from Foundation import NSDate, NSMetadataQuery, NSPredicate, NSRunLoop
# pylint: enable=E0611

from . import display
from . import munkilog
from . import osutils
from . import pkgutils
from . import prefs
from .. import FoundationPlist

# we use lots of camelCase-style names. Deal with it.
# pylint: disable=C0103


# Always ignore these directories when discovering applications.
APP_DISCOVERY_EXCLUSION_DIRS = set([
    'Volumes', 'tmp', '.vol', '.Trashes', '.MobileBackups', '.Spotlight-V100',
    '.fseventsd', 'Network', 'net', 'home', 'cores', 'dev', 'private',
    ])


class Memoize(dict):
    '''Class to cache the return values of an expensive function.
    This version supports only functions with non-keyword arguments'''
    def __init__(self, func):
        self.func = func

    def __call__(self, *args):
        return self[args]

    def __missing__(self, key):
        result = self[key] = self.func(*key)
        return result


class Error(Exception):
    """Class for domain specific exceptions."""


class TimeoutError(Error):
    """Timeout limit exceeded since last I/O."""


def set_file_nonblock(f, non_blocking=True):
    """Set non-blocking flag on a file object.

    Args:
      f: file
      non_blocking: bool, default True, non-blocking mode or not
    """
    flags = fcntl.fcntl(f.fileno(), fcntl.F_GETFL)
    if bool(flags & os.O_NONBLOCK) != non_blocking:
        flags ^= os.O_NONBLOCK
    fcntl.fcntl(f.fileno(), fcntl.F_SETFL, flags)


class Popen(subprocess.Popen):
    """Subclass of subprocess.Popen to add support for timeouts."""

    def timed_readline(self, f, timeout):
        """Perform readline-like operation with timeout.

        Args:
            f: file object to .readline() on
            timeout: int, seconds of inactivity to raise error at
        Raises:
            TimeoutError, if timeout is reached
        """
        set_file_nonblock(f)

        output = []
        inactive = 0
        while 1:
            (rlist, dummy_wlist, dummy_xlist) = select.select(
                [f], [], [], 1.0)

            if not rlist:
                inactive += 1  # approx -- py select doesn't return tv
                if inactive >= timeout:
                    break
            else:
                inactive = 0
                c = f.read(1)
                output.append(c)  # keep newline
                if c == '' or c == '\n':
                    break

        set_file_nonblock(f, non_blocking=False)

        if inactive >= timeout:
            raise TimeoutError  # note, an incomplete line can be lost
        else:
            return ''.join(output)

    def communicate(self, std_in=None, timeout=0):
        """Communicate, optionally ending after a timeout of no activity.

        Args:
            std_in: str, to send on stdin
            timeout: int, seconds of inactivity to raise error at
        Returns:
            (str or None, str or None) for stdout, stderr
        Raises:
            TimeoutError, if timeout is reached
        """
        if timeout <= 0:
            return super(Popen, self).communicate(input=std_in)

        fds = []
        stdout = []
        stderr = []

        if self.stdout is not None:
            set_file_nonblock(self.stdout)
            fds.append(self.stdout)
        if self.stderr is not None:
            set_file_nonblock(self.stderr)
            fds.append(self.stderr)

        if std_in is not None and sys.stdin is not None:
            sys.stdin.write(std_in)

        returncode = None
        inactive = 0
        while returncode is None:
            (rlist, dummy_wlist, dummy_xlist) = select.select(
                fds, [], [], 1.0)

            if not rlist:
                inactive += 1
                if inactive >= timeout:
                    raise TimeoutError
            else:
                inactive = 0
                for fd in rlist:
                    if fd is self.stdout:
                        stdout.append(fd.read())
                    elif fd is self.stderr:
                        stderr.append(fd.read())

            returncode = self.poll()

        if self.stdout is not None:
            stdout = ''.join(stdout)
        else:
            stdout = None
        if self.stderr is not None:
            stderr = ''.join(stderr)
        else:
            stderr = None

        return (stdout, stderr)


def _unsigned(i):
    """Translate a signed int into an unsigned int.  Int type returned
    is longer than the original since Python has no unsigned int."""
    return i & 0xFFFFFFFF


def _asciizToStr(a_string):
    """Transform a null-terminated string of any length into a Python str.
    Returns a normal Python str that has been terminated.
    """
    i = a_string.find('\0')
    if i > -1:
        a_string = a_string[0:i]
    return a_string


def _fFlagsToSet(f_flags):
    """Transform an int f_flags parameter into a set of mount options.
    Returns a set.
    """
    # see /usr/include/sys/mount.h for the bitmask constants.
    flags = set()
    if f_flags & 0x1:
        flags.add('read-only')
    if f_flags & 0x1000:
        flags.add('local')
    if f_flags & 0x4000:
        flags.add('rootfs')
    if f_flags & 0x4000000:
        flags.add('automounted')
    return flags


def getFilesystems():
    """Get a list of all mounted filesystems on this system.

    Return value is dict, e.g. {
        int st_dev: {
            'f_fstypename': 'nfs',
            'f_mntonname': '/mountedpath',
            'f_mntfromname': 'homenfs:/path',
        },
    }

    Note: st_dev values are static for potentially only one boot, but
    static for multiple mount instances.
    """

    MNT_NOWAIT = 2

    libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c"))
    # see man GETFSSTAT(2) for struct
    statfs_32_struct = '=hh ll ll ll lQ lh hl 2l 15s 90s 90s x 16x'
    statfs_64_struct = '=Ll QQ QQ Q ll l LLL 16s 1024s 1024s 32x'
    os_version = osutils.getOsVersion(as_tuple=True)
    if os_version <= (10, 5):
        mode = 32
    else:
        mode = 64

    if mode == 64:
        statfs_struct = statfs_64_struct
    else:
        statfs_struct = statfs_32_struct

    sizeof_statfs_struct = struct.calcsize(statfs_struct)
    bufsize = 30 * sizeof_statfs_struct  # only supports 30 mounted fs
    buf = ctypes.create_string_buffer(bufsize)

    if mode == 64:
        # some 10.6 boxes return 64-bit structures on getfsstat(), some do not.
        # forcefully call the 64-bit version in cases where we think
        # a 64-bit struct will be returned.
        n = libc.getfsstat64(ctypes.byref(buf), bufsize, MNT_NOWAIT)
    else:
        n = libc.getfsstat(ctypes.byref(buf), bufsize, MNT_NOWAIT)

    if n < 0:
        display.display_debug1('getfsstat() returned errno %d' % n)
        return {}

    ofs = 0
    output = {}
    # struct_unpack returns lots of values, but we use only a few
    # pylint: disable=unused-variable
    for i in xrange(0, n):
        if mode == 64:
            (f_bsize, f_iosize, f_blocks, f_bfree, f_bavail, f_files,
             f_ffree, f_fsid_0, f_fsid_1, f_owner, f_type, f_flags,
             f_fssubtype,
             f_fstypename, f_mntonname, f_mntfromname) = struct.unpack(
                 statfs_struct, str(buf[ofs:ofs+sizeof_statfs_struct]))
        elif mode == 32:
            (f_otype, f_oflags, f_bsize, f_iosize, f_blocks, f_bfree, f_bavail,
             f_files, f_ffree, f_fsid, f_owner, f_reserved1, f_type, f_flags,
             f_reserved2_0, f_reserved2_1, f_fstypename, f_mntonname,
             f_mntfromname) = struct.unpack(
                 statfs_struct, str(buf[ofs:ofs+sizeof_statfs_struct]))

        try:
            st = os.stat(_asciizToStr(f_mntonname))
            output[st.st_dev] = {
                'f_flags_set': _fFlagsToSet(f_flags),
                'f_fstypename': _asciizToStr(f_fstypename),
                'f_mntonname': _asciizToStr(f_mntonname),
                'f_mntfromname': _asciizToStr(f_mntfromname),
            }
        except OSError:
            pass

        ofs += sizeof_statfs_struct
    # pylint: enable=unused-variable

    return output


FILESYSTEMS = {}
def isExcludedFilesystem(path, _retry=False):
    """Gets filesystem information for a path and determine if it should be
    excluded from application searches.

    Returns True if path is located on NFS, is read only, or
    is not marked local.
    Returns False if none of these conditions are true.
    Returns None if it cannot be determined.
    """
    global FILESYSTEMS

    if not path:
        return None

    path_components = path.split('/')
    if len(path_components) > 1:
        if path_components[1] in APP_DISCOVERY_EXCLUSION_DIRS:
            return True

    if not FILESYSTEMS or _retry:
        FILESYSTEMS = getFilesystems()

    try:
        st = os.stat(path)
    except OSError:
        st = None

    if st is None or st.st_dev not in FILESYSTEMS:
        if not _retry:
            # perhaps the stat() on the path caused autofs to mount
            # the required filesystem and now it will be available.
            # try one more time to look for it after flushing the cache.
            display.display_debug1(
                'Trying isExcludedFilesystem again for %s' % path)
            return isExcludedFilesystem(path, True)
        else:
            display.display_debug1(
                'Could not match path %s to a filesystem' % path)
            return None

    exc_flags = ('read-only' in FILESYSTEMS[st.st_dev]['f_flags_set'] or
                 'local' not in FILESYSTEMS[st.st_dev]['f_flags_set'])
    is_nfs = FILESYSTEMS[st.st_dev]['f_fstypename'] == 'nfs'

    if is_nfs or exc_flags:
        display.display_debug1(
            'Excluding %s (flags %s, nfs %s)' % (path, exc_flags, is_nfs))

    return is_nfs or exc_flags


def findAppsInDirs(dirlist):
    """Do spotlight search for type applications within the
    list of directories provided. Returns a list of paths to applications
    these appear to always be some form of unicode string.
    """
    applist = []
    query = NSMetadataQuery.alloc().init()
    query.setPredicate_(
        NSPredicate.predicateWithFormat_('(kMDItemKind = "Application")'))
    query.setSearchScopes_(dirlist)
    query.startQuery()
    # Spotlight isGathering phase - this is the initial search. After the
    # isGathering phase Spotlight keeps running returning live results from
    # filesystem changes, we are not interested in that phase.
    # Run for 0.3 seconds then check if isGathering has completed.
    runtime = 0
    maxruntime = 20
    while query.isGathering() and runtime <= maxruntime:
        runtime += 0.3
        NSRunLoop.currentRunLoop(
            ).runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.3))
    query.stopQuery()

    if runtime >= maxruntime:
        display.display_warning(
            'Spotlight search for applications terminated due to excessive '
            'time. Possible causes: Spotlight indexing is turned off for a '
            'volume; Spotlight is reindexing a volume.')

    for item in query.results():
        p = item.valueForAttribute_('kMDItemPath')
        if p and not isExcludedFilesystem(p):
            applist.append(p)

    return applist


def getSpotlightInstalledApplications():
    """Get paths of currently installed applications per Spotlight.
    Return value is list of paths.
    Excludes most non-boot volumes.
    In future may include local r/w volumes.
    """
    dirlist = []
    applist = []

    for f in osutils.listdir(u'/'):
        p = os.path.join(u'/', f)
        if os.path.isdir(p) and not os.path.islink(p) \
                            and not isExcludedFilesystem(p):
            if f.endswith('.app'):
                applist.append(p)
            else:
                dirlist.append(p)

    # Future code changes may mean we wish to look for Applications
    # installed on any r/w local volume.
    #for f in osutils.listdir(u'/Volumes'):
    #    p = os.path.join(u'/Volumes', f)
    #    if os.path.isdir(p) and not os.path.islink(p) \
    #                        and not isExcludedFilesystem(p):
    #        dirlist.append(p)

    # /Users is not currently excluded, so no need to add /Users/Shared.
    #dirlist.append(u'/Users/Shared')

    applist.extend(findAppsInDirs(dirlist))
    return applist


def getLSInstalledApplications():
    """Get paths of currently installed applications per LaunchServices.
    Return value is list of paths.
    Ignores apps installed on other volumes
    """
    # PyLint cannot properly find names inside Cocoa libraries, so issues bogus
    # "Module 'Foo' has no 'Bar' member" warnings. Disable them.
    # pylint: disable=E1101
    # we access a "protected" function from LaunchServices
    # pylint: disable=W0212

    apps = LaunchServices._LSCopyAllApplicationURLs(None)
    applist = []
    for app in apps:
        app_path = app.path()
        if (app_path and not isExcludedFilesystem(app_path) and
                os.path.exists(app_path)):
            applist.append(app_path)

    return applist


@Memoize
def getSPApplicationData():
    '''Uses system profiler to get application info for this machine'''
    cmd = ['/usr/sbin/system_profiler', 'SPApplicationsDataType', '-xml']
    # uses our internal Popen instead of subprocess's so we can timeout
    proc = Popen(cmd, shell=False, bufsize=-1,
                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE)
    try:
        output, dummy_error = proc.communicate(timeout=60)
    except TimeoutError:
        display.display_error(
            'system_profiler hung; skipping SPApplicationsDataType query')
        # return empty dict
        return {}
    try:
        plist = FoundationPlist.readPlistFromString(output)
        # system_profiler xml is an array
        app_data = {}
        for item in plist[0]['_items']:
            app_data[item.get('path')] = item
    except BaseException:
        app_data = {}
    return app_data


@Memoize
def getAppData():
    """Gets info on currently installed apps.
    Returns a list of dicts containing path, name, version and bundleid"""
    app_data = []
    display.display_debug1(
        'Getting info on currently installed applications...')
    applist = set(getLSInstalledApplications())
    applist.update(getSpotlightInstalledApplications())
    for pathname in applist:
        iteminfo = {}
        iteminfo['name'] = os.path.splitext(os.path.basename(pathname))[0]
        iteminfo['path'] = pathname
        plistpath = os.path.join(pathname, 'Contents', 'Info.plist')
        if os.path.exists(plistpath):
            try:
                plist = FoundationPlist.readPlist(plistpath)
                iteminfo['bundleid'] = plist.get('CFBundleIdentifier', '')
                if 'CFBundleName' in plist:
                    iteminfo['name'] = plist['CFBundleName']
                iteminfo['version'] = pkgutils.getBundleVersion(pathname)
                app_data.append(iteminfo)
            except BaseException:
                pass
        else:
            # possibly a non-bundle app. Use system_profiler data
            # to get app name and version
            sp_app_data = getSPApplicationData()
            if pathname in sp_app_data:
                item = sp_app_data[pathname]
                iteminfo['bundleid'] = ''
                iteminfo['version'] = item.get('version') or '0.0.0.0.0'
                if item.get('_name'):
                    iteminfo['name'] = item['_name']
                app_data.append(iteminfo)
    return app_data


def get_version():
    """Returns version of munkitools, reading version.plist"""
    vers = "UNKNOWN"
    build = ""
    # find the munkilib directory, and the version file
    munkilibdir = os.path.dirname(os.path.abspath(__file__))
    versionfile = os.path.join(munkilibdir, "version.plist")
    if os.path.exists(versionfile):
        try:
            vers_plist = FoundationPlist.readPlist(versionfile)
        except FoundationPlist.NSPropertyListSerializationException:
            pass
        else:
            try:
                vers = vers_plist['CFBundleShortVersionString']
                build = vers_plist['BuildNumber']
            except KeyError:
                pass
    if build:
        vers = vers + "." + build
    return vers


def get_hardware_info():
    '''Uses system profiler to get hardware info for this machine'''
    cmd = ['/usr/sbin/system_profiler', 'SPHardwareDataType', '-xml']
    proc = subprocess.Popen(cmd, shell=False, bufsize=-1,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, dummy_error) = proc.communicate()
    try:
        plist = FoundationPlist.readPlistFromString(output)
        # system_profiler xml is an array
        sp_dict = plist[0]
        items = sp_dict['_items']
        sp_hardware_dict = items[0]
        return sp_hardware_dict
    except BaseException:
        return {}


def get_ip_addresses(kind):
    '''Uses system profiler to get active IP addresses for this machine
    kind must be one of 'IPv4' or 'IPv6' '''
    ip_addresses = []
    cmd = ['/usr/sbin/system_profiler', 'SPNetworkDataType', '-xml']
    proc = subprocess.Popen(cmd, shell=False, bufsize=-1,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, dummy_error) = proc.communicate()
    try:
        plist = FoundationPlist.readPlistFromString(output)
        # system_profiler xml is an array of length 1
        sp_dict = plist[0]
        items = sp_dict['_items']
    except BaseException:
        # something is wrong with system_profiler output
        # so bail
        return ip_addresses

    for item in items:
        try:
            ip_addresses.extend(item[kind]['Addresses'])
        except KeyError:
            # 'IPv4", 'IPv6' or 'Addresses' is empty, so we ignore
            # this item
            pass
    return ip_addresses


def getIntel64Support():
    """Does this machine support 64-bit Intel instruction set?"""
    libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c"))

    size = ctypes.c_size_t()
    buf = ctypes.c_int()
    size.value = ctypes.sizeof(buf)

    libc.sysctlbyname(
        "hw.optional.x86_64", ctypes.byref(buf), ctypes.byref(size), None, 0)

    return buf.value == 1


def getAvailableDiskSpace(volumepath='/'):
    """Returns available diskspace in KBytes.

    Args:
      volumepath: str, optional, default '/'
    Returns:
      int, KBytes in free space available
    """
    if volumepath is None:
        volumepath = '/'
    try:
        st = os.statvfs(volumepath)
    except OSError, e:
        display.display_error(
            'Error getting disk space in %s: %s', volumepath, str(e))
        return 0

    return int(st.f_frsize * st.f_bavail / 1024) # f_bavail matches df(1) output


@Memoize
def getMachineFacts():
    """Gets some facts about this machine we use to determine if a given
    installer is applicable to this OS or hardware"""
    machine = dict()
    machine['hostname'] = os.uname()[1]
    machine['arch'] = os.uname()[4]
    machine['os_vers'] = osutils.getOsVersion(only_major_minor=False)
    hardware_info = get_hardware_info()
    machine['machine_model'] = hardware_info.get('machine_model', 'UNKNOWN')
    machine['munki_version'] = get_version()
    machine['ipv4_address'] = get_ip_addresses('IPv4')
    machine['ipv6_address'] = get_ip_addresses('IPv6')
    machine['serial_number'] = hardware_info.get('serial_number', 'UNKNOWN')

    if machine['arch'] == 'x86_64':
        machine['x86_64_capable'] = True
    elif machine['arch'] == 'i386':
        machine['x86_64_capable'] = getIntel64Support()
    return machine


def validPlist(path):
    """Uses plutil to determine if path contains a valid plist.
    Returns True or False."""
    retcode = subprocess.call(['/usr/bin/plutil', '-lint', '-s', path])
    return retcode == 0


@Memoize
def getConditions():
    """Fetches key/value pairs from condition scripts
    which can be placed into /usr/local/munki/conditions"""
    # define path to conditions directory which would contain
    # admin created scripts
    scriptdir = os.path.realpath(os.path.dirname(sys.argv[0]))
    conditionalscriptdir = os.path.join(scriptdir, "conditions")
    # define path to ConditionalItems.plist
    conditionalitemspath = os.path.join(
        prefs.pref('ManagedInstallDir'), 'ConditionalItems.plist')
    try:
        # delete CondtionalItems.plist so that we're starting fresh
        os.unlink(conditionalitemspath)
    except (OSError, IOError):
        pass
    if os.path.exists(conditionalscriptdir):
        from munkilib import utils
        for conditionalscript in osutils.listdir(conditionalscriptdir):
            if conditionalscript.startswith('.'):
                # skip files that start with a period
                continue
            conditionalscriptpath = os.path.join(
                conditionalscriptdir, conditionalscript)
            if os.path.isdir(conditionalscriptpath):
                # skip directories in conditions directory
                continue
            try:
                # attempt to execute condition script
                dummy_result, dummy_stdout, dummy_stderr = (
                    utils.runExternalScript(conditionalscriptpath))
            except utils.ScriptNotFoundError:
                pass  # script is not required, so pass
            except utils.RunExternalScriptError, err:
                print >> sys.stderr, unicode(err)
    else:
        # /usr/local/munki/conditions does not exist
        pass
    if (os.path.exists(conditionalitemspath) and
            validPlist(conditionalitemspath)):
        # import conditions into conditions dict
        conditions = FoundationPlist.readPlist(conditionalitemspath)
        os.unlink(conditionalitemspath)
    else:
        # either ConditionalItems.plist does not exist
        # or does not pass validation
        conditions = {}
    return conditions


def saveappdata():
    """Save installed application data"""
    # data from getAppData() is meant for use by updatecheck
    # we need to massage it a bit for more general usage
    munkilog.log('Saving application inventory...')
    app_inventory = []
    for item in getAppData():
        inventory_item = {}
        inventory_item['CFBundleName'] = item.get('name')
        inventory_item['bundleid'] = item.get('bundleid')
        inventory_item['version'] = item.get('version')
        inventory_item['path'] = item.get('path', '')
        # use last path item (minus '.app' if present) as name
        inventory_item['name'] = \
            os.path.splitext(os.path.basename(inventory_item['path']))[0]
        app_inventory.append(inventory_item)
    try:
        FoundationPlist.writePlist(
            app_inventory,
            os.path.join(
                prefs.pref('ManagedInstallDir'), 'ApplicationInventory.plist'))
    except FoundationPlist.NSPropertyListSerializationException, err:
        display.display_warning(
            'Unable to save inventory report: %s' % err)


if __name__ == '__main__':
    print 'This is a library of support tools for the Munki Suite.'
