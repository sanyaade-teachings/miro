# Miro - an RSS based video player application
# Copyright (C) 2005-2009 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

import os
import random
import logging
from base64 import b64encode

from miro.gtcache import gettext as _
from miro.database import DDBObject, ObjectNotFoundError
from miro.dl_daemon import daemon, command
from miro.download_utils import nextFreeFilename, getFileURLPath, filterDirectoryName
from miro.util import get_torrent_info_hash, returnsUnicode, checkU, returnsFilename, unicodify, checkF, toUni
from miro import config
from miro import dialogs
from miro import httpclient
from miro import prefs
from miro.plat.utils import samefile, FilenameType, unicodeToFilename
from miro import flashscraper
from miro import fileutil

daemon_starter = None

# a hash of download ids that the server knows about.
_downloads = {}

# Returns an HTTP auth object corresponding to the given host, path or
# None if it doesn't exist
def find_http_auth(host, path, realm=None, scheme=None):
    checkU(host)
    checkU(path)
    if realm:
        checkU(realm)
    if scheme:
        checkU(scheme)
    #print "Trying to find HTTPAuth with host %s, path %s, realm %s, and scheme %s" %(host,path,realm,scheme)
    for obj in HTTPAuthPassword.make_view():
        if (obj.host == host and path.startswith(obj.path) and
                (realm is None or obj.realm == realm) and
                (scheme is None or obj.authScheme == scheme)):
            return obj
    return None


class HTTPAuthPassword(DDBObject):
    def setup_new(self, username, password, host, realm, path, authScheme=u"Basic"):
        checkU(username)
        checkU(password)
        checkU(host)
        checkU(realm)
        checkU(path)
        checkU(authScheme)
        oldAuth = find_http_auth(host, path, realm, authScheme)
        while not oldAuth is None:
            oldAuth.remove()
            oldAuth = find_http_auth(host, path, realm, authScheme)
        self.username = username
        self.password = password
        self.host = host
        self.realm = realm
        self.path = os.path.dirname(path)
        self.authScheme = authScheme

    def get_auth_token(self):
        authString = u':'
        self.confirm_db_thread()
        authString = self.username+u':'+self.password
        return b64encode(authString)

    def get_auth_scheme(self):
        self.confirm_db_thread()
        return self.authScheme

totalUpRate = 0
totalDownRate = 0

def get_downloader_by_dlid(dlid):
    try:
        return RemoteDownloader.get_by_dlid(dlid)
    except ObjectNotFoundError:
        return None

@returnsUnicode
def generate_dlid():
    dlid = u"download%08d" % random.randint(0, 99999999)
    while get_downloader_by_dlid(dlid=dlid):
        dlid = u"download%08d" % random.randint(0, 99999999)
    return dlid

class RemoteDownloader(DDBObject):
    """Download a file using the downloader daemon."""
    def setup_new(self, url, item, contentType=None, channelName=None):
        checkU(url)
        if contentType:
            checkU(contentType)
        self.origURL = self.url = url
        self.itemList = []
        self.child_deleted = False
        self.main_item_id = None
        self.dlid = generate_dlid()
        self.status = {}
        self.state = u'downloading'
        if contentType is None:
            # HACK:  Some servers report the wrong content-type for torrent
            # files.  We try to work around that by assuming if the enclosure
            # states that something is a torrent, it's a torrent.
            # Thanks to j@v2v.cc
            if item.enclosure_type == u'application/x-bittorrent':
                contentType = item.enclosure_type
        self.contentType = u""
        self.deleteFiles = True
        self.channelName = channelName
        self.manualUpload = False
        if contentType is None:
            self.contentType = u""
        else:
            self.contentType = contentType

        if self.contentType == u'':
            self.get_content_type()
        else:
            self.run_downloader()

    @classmethod
    def finished_view(cls):
        return cls.make_view("state in ('finished', 'uploading', "
                "'uploading-paused')")

    @classmethod
    def auto_uploader_view(cls):
        return cls.make_view("state == 'uploading' AND NOT manualUpload")

    @classmethod
    def get_by_dlid(cls, dlid):
        return cls.make_view('dlid=?', (dlid,)).get_singleton()

    @classmethod
    def get_by_url(cls, url):
        return cls.make_view('origURL=?', (url,)).get_singleton()

    @classmethod
    def orphaned_view(cls):
        """Downloaders with no items associated with them."""
        return cls.make_view('id NOT IN (SELECT downloader_id from item)')

    def signal_change(self, needsSave=True, needsSignalItem=True):
        DDBObject.signal_change(self, needsSave=needsSave)
        if needsSignalItem:
            for item in self.itemList:
                item.signal_change(needsSave=False)

    def on_content_type(self, info):
        if not self.idExists():
            return

        if info['status'] == 200:
            self.url = info['updated-url'].decode('ascii','replace')
            self.contentType = None
            try:
                self.contentType = info['content-type'].decode('ascii','replace')
            except (SystemExit, KeyboardInterrupt):
                raise
            except:
                self.contentType = None
            self.run_downloader()
        else:
            error = httpclient.UnexpectedStatusCode(info['status'])
            self.on_content_type_error(error)

    def on_content_type_error(self, error):
        if not self.idExists():
            return

        self.status['state'] = u"failed"
        self.status['shortReasonFailed'] = error.getFriendlyDescription()
        self.status['reasonFailed'] = error.getLongDescription()
        self.signal_change()

    def get_content_type(self):
        httpclient.grabHeaders(self.url,
                               self.on_content_type, self.on_content_type_error)
 
    @classmethod
    def initialize_daemon(cls):
        RemoteDownloader.dldaemon = daemon.ControllerDaemon()

    def _get_rates(self):
        state = self.get_state()
        if state == u'downloading':
            return (self.status.get('rate', 0), self.status.get('upRate', 0))
        if state == u'uploading':
            return (0, self.status.get('upRate', 0))
        return (0, 0)

    def before_changing_status(self):
        global totalDownRate
        global totalUpRate
        rates = self._get_rates()
        totalDownRate -= rates[0]
        totalUpRate -= rates[1]

    def after_changing_status(self):
        global totalDownRate
        global totalUpRate
        rates = self._get_rates()
        totalDownRate += rates[0]
        totalUpRate += rates[1]

    @classmethod
    def update_status(cls, data):
        for field in data:
            if field not in ['filename', 'shortFilename', 'channelName', 'metainfo', 'fastResumeData']:
                data[field] = unicodify(data[field])
        self = get_downloader_by_dlid(dlid=data['dlid'])
        # print data
        if self is not None:
            # FIXME - this should get fixed.
            try:
                if self.status == data:
                    return
            except Exception:
                # This is a known bug with the way we used to save fast resume
                # data
                logging.exception("RemoteDownloader.update_status: exception when comparing status")

            wasFinished = self.isFinished()
            old_filename = self.get_filename()
            self.before_changing_status()

            # FIXME: how do we get all of the possible bit torrent
            # activity strings into gettext? --NN
            if data.has_key('activity') and data['activity']:
                data['activity'] = _(data['activity'])

            self.status = data
            self._recalc_state()

            # Store the time the download finished
            finished = self.isFinished() and not wasFinished
            file_migrated = (self.isFinished() and
                    self.get_filename() != old_filename)
            needsSignalItem = not (finished or file_migrated)
            self.after_changing_status()

            if self.get_state() == u'uploading' and not self.manualUpload and self.getUploadRatio() > 1.5:
                self.stopUpload()

            self.signal_change(needsSignalItem = needsSignalItem)
            if finished:
                for item in self.itemList:
                    item.on_download_finished()
            elif file_migrated:
                self._file_migrated(old_filename)

    def run_downloader(self):
        """This is the actual download thread.
        """
        flashscraper.try_scraping_url(self.url, self._run_downloader)

    def _run_downloader(self, url, contentType = None):
        if not self.idExists():
            return # we got deleted while we were doing the flash scraping
        if contentType is not None:
            self.contentType = contentType
        if url is not None:
            self.url = url
            logging.debug("downloading url %s", self.url)
            c = command.StartNewDownloadCommand(RemoteDownloader.dldaemon,
                                                self.url, self.dlid, self.contentType, self.channelName)
            c.send()
            _downloads[self.dlid] = self
        else:
            self.status["state"] = u'failed'
            self.status["shortReasonFailed"] = _('File not found')
            self.status["reasonFailed"] = _('Flash URL Scraping Error')
        self.signal_change()

    def pause(self):
        """Pauses the download."""
        if _downloads.has_key(self.dlid):
            c = command.PauseDownloadCommand(RemoteDownloader.dldaemon, self.dlid)
            c.send()
        else:
            self.before_changing_status()
            self.status["state"] = u"paused"
            self.after_changing_status()
            self.signal_change()

    def stop(self, delete):
        """Stops the download and removes the partially downloaded
        file.
        """
        if self.get_state() in [u'downloading', u'uploading', u'paused']:
            if _downloads.has_key(self.dlid):
                c = command.StopDownloadCommand(RemoteDownloader.dldaemon,
                                                self.dlid, delete)
                c.send()
                del _downloads[self.dlid]
        else:
            if delete:
                self.delete()
            self.status["state"] = u"stopped"
            self.signal_change()

    def delete(self):
        try:
            filename = self.status['filename']
        except KeyError:
            return
        try:
            fileutil.delete(filename)
        except OSError:
            logging.exception("Error deleting downloaded file: %s", toUni(filename))

        parent = os.path.join(fileutil.expand_filename(filename), os.path.pardir)
        parent = os.path.normpath(parent)
        moviesDir = fileutil.expand_filename(config.get(prefs.MOVIES_DIRECTORY))
        if (os.path.exists(parent) and os.path.exists(moviesDir) and
                not samefile(parent, moviesDir) and
                len(os.listdir(parent)) == 0):
            try:
                os.rmdir(parent)
            except OSError:
                logging.exception("Error deleting empty download directory: %s", toUni(parent))

    def start(self):
        """Continues a paused, stopped, or failed download thread
        """
        if self.get_state() == u'failed':
            if _downloads.has_key (self.dlid):
                del _downloads[self.dlid]
            self.dlid = generate_dlid()
            self.before_changing_status()
            self.status = {}
            self.after_changing_status()
            if self.contentType == u"":
                self.get_content_type()
            else:
                self.run_downloader()
            self.signal_change()
        elif self.get_state() in (u'stopped', u'paused', u'offline'):
            if _downloads.has_key(self.dlid):
                c = command.StartDownloadCommand(RemoteDownloader.dldaemon,
                                                 self.dlid)
                c.send()
            else:
                self.status['state'] = u'downloading'
                self.restart()
                self.signal_change()

    def migrate(self, directory):
        if _downloads.has_key(self.dlid):
            c = command.MigrateDownloadCommand(RemoteDownloader.dldaemon,
                                               self.dlid, directory)
            c.send()
        else:
            # downloader doesn't have our dlid.  Move the file ourself.
            try:
                shortFilename = self.status['shortFilename']
            except KeyError:
                print """\
WARNING: can't migrate download because we don't have a shortFilename!
URL was %s""" % self.url
                return
            try:
                filename = self.status['filename']
            except KeyError:
                print """\
WARNING: can't migrate download because we don't have a filename!
URL was %s""" % self.url
                return
            if fileutil.exists(filename):
                if self.status.get('channelName', None) is not None:
                    channelName = filterDirectoryName(self.status['channelName'])
                    directory = os.path.join(directory, channelName)
                try:
                    fileutil.makedirs(directory)
                except OSError:
                    pass
                newfilename = os.path.join(directory, shortFilename)
                if newfilename == filename:
                    return
                newfilename = nextFreeFilename(newfilename)
                def callback():
                    self.status['filename'] = newfilename
                    self.signal_change(needsSignalItem=False)
                    self._file_migrated(filename)
                fileutil.migrate_file(filename, newfilename, callback)
        for i in self.itemList:
            i.migrate_children(directory)

    def _file_migrated(self, old_filename):
        for item in self.itemList:
            item.on_downloader_migrated(old_filename, self.get_filename())

    def set_delete_files(self, deleteFiles):
        self.deleteFiles = deleteFiles

    def set_channel_name(self, channelName):
        if self.channelName is None:
            if channelName:
                checkF(channelName)
            self.channelName = channelName

    def remove(self):
        """Removes downloader from the database and deletes the file.
        """
        global totalDownRate
        global totalUpRate
        rates = self._get_rates()
        totalDownRate -= rates[0]
        totalUpRate -= rates[1]
        self.stop(self.deleteFiles)
        DDBObject.remove(self)

    def get_type(self):
        """Get the type of download.  Will return either "http" or
        "bittorrent".
        """
        self.confirm_db_thread()
        if self.contentType == u'application/x-bittorrent':
            return u"bittorrent"
        else:
            return u"http"

    def addItem(self, item):
        """In case multiple downloaders are getting the same file, we can support
        multiple items
        """
        if item not in self.itemList:
            self.itemList.append(item)
            if self.main_item_id is None:
                self.main_item_id = item.id
                self.signal_change()

    def removeItem(self, item):
        self.itemList.remove(item)
        if len (self.itemList) == 0:
            self.remove()
        elif item.id == self.main_item_id:
            self.main_item_id = self.itemList[0].id
            self.signal_change()

    def getRate(self):
        self.confirm_db_thread()
        return self.status.get('rate', 0)

    def getETA(self):
        self.confirm_db_thread()
        return self.status.get('eta', 0)

    @returnsUnicode
    def get_startup_activity(self):
        self.confirm_db_thread()
        activity = self.status.get('activity')
        if activity is None:
            return _("starting up")
        else:
            return activity

    @returnsUnicode
    def getReasonFailed(self):
        """Returns the reason for the failure of this download
        This should only be called when the download is in the failed state
        """
        if not self.get_state() == u'failed':
            msg = u"getReasonFailed() called on a non-failed downloader"
            raise ValueError(msg)
        self.confirm_db_thread()
        return self.status.get('reasonFailed', _("Unknown"))

    @returnsUnicode
    def getShortReasonFailed(self):
        if not self.get_state() == u'failed':
            msg = u"getShortReasonFailed() called on a non-failed downloader"
            raise ValueError(msg)
        self.confirm_db_thread()
        return self.status.get('shortReasonFailed', _("Unknown"))

    @returnsUnicode
    def get_url(self):
        """Returns the URL we're downloading
        """
        self.confirm_db_thread()
        return self.url

    @returnsUnicode    
    def get_state(self):
        """Returns the state of the download: downloading, paused, stopped,
        failed, or finished
        """
        self.confirm_db_thread()
        return self.state

    def isFinished(self):
        return self.get_state() in (u'finished', u'uploading', u'uploading-paused')

    def getTotalSize(self):
        """Returns the total size of the download in bytes
        """
        self.confirm_db_thread()
        return self.status.get('totalSize', -1)

    def get_current_size(self):
        """Returns the current amount downloaded in bytes
        """
        self.confirm_db_thread()
        return self.status.get('currentSize', 0)

    @returnsFilename
    def get_filename(self):
        """Returns the filename that we're downloading to. Should not be
        called until state is "finished."
        """
        self.confirm_db_thread()
        # FIXME - FilenameType('') is a bogus value, but looks like a filename.
        # should return None.
        return self.status.get('filename', FilenameType(''))

    def setup_restored(self):
        self.deleteFiles = True
        self.itemList = []
        if self.dlid == 'noid':
            # this won't happen nowadays, but it can for old databases
            self.dlid = generate_dlid()
        self.status['rate'] = 0
        self.status['upRate'] = 0
        self.status['eta'] = 0

    def on_signal_change(self):
        self._recalc_state()

    def _recalc_state(self):
        self.state = self.status.get('state', u'downloading')

    def getUploadRatio(self):
        size = self.get_current_size()
        if size == 0:
            return 0
        return self.status.get('uploaded', 0) / size
    
    def restartIfNeeded(self):
        if self.get_state() in (u'downloading', u'offline'):
            self.restart()
        if self.get_state() in (u'uploading'):
            if self.manualUpload or self.getUploadRatio() < 1.5:
                self.restart()
            else:
                self.stopUpload()

    def restart(self):
        if not self.status or self.status.get('dlerType') is None:
            if self.contentType == u"":
                self.get_content_type()
            else:
                self.run_downloader()
        else:
            _downloads[self.dlid] = self
            c = command.RestoreDownloaderCommand(RemoteDownloader.dldaemon, 
                                                 self.status)
            c.send()

    def startUpload(self):
        if self.get_type() != u'bittorrent':
            logging.warn("called startUpload for non-bittorrent downloader")
            return
        if self.child_deleted:
            title = "Can't Resume Seeding"
            msg = ("Seeding cannot resume because part of this torrent "
                    "has been deleted.")
            dialogs.MessageBoxDialog(title, msg).run()
            return
        if self.get_state() not in (u'finished', u'uploading-paused'):
            logging.warn("called startUpload when downloader state is: %s",
                         self.get_state())
            return
        self.manualUpload = True
        if _downloads.has_key(self.dlid):
            c = command.StartDownloadCommand(RemoteDownloader.dldaemon,
                                             self.dlid)
            c.send()
        else:
            self.before_changing_status()
            self.status['state'] = u'uploading'
            self.after_changing_status()
            self.restart()
            self.signal_change()

    def stopUpload(self):
        """
        Stop uploading/seeding and set status as "finished".
        """
        if _downloads.has_key(self.dlid):
            c = command.StopUploadCommand(RemoteDownloader.dldaemon,
                                          self.dlid)
            c.send()
            del _downloads[self.dlid]
        self.before_changing_status()
        self.status["state"] = u"finished"
        self.after_changing_status()
        self.signal_change()

    def pauseUpload(self):
        """
        Stop uploading/seeding and set status as "uploading-paused".
        """
        if _downloads.has_key(self.dlid):
            c = command.PauseUploadCommand(RemoteDownloader.dldaemon,
                                           self.dlid)
            c.send()
            del _downloads[self.dlid]
        self.before_changing_status()
        self.status["state"] = u"uploading-paused"
        self.after_changing_status()
        self.signal_change()


def cleanup_incomplete_downloads():
    downloadDir = os.path.join(config.get(prefs.MOVIES_DIRECTORY),
                                          'Incomplete Downloads')
    if not fileutil.exists(downloadDir):
        return

    filesInUse = set()
    for downloader in RemoteDownloader.make_view():
        if downloader.get_state() in ('downloading', 'paused',
                                     'offline', 'uploading', 'finished',
                                     'uploading-paused'):
            filename = downloader.get_filename()
            if len(filename) > 0:
                if not fileutil.isabs(filename):
                    filename = os.path.join(downloadDir, filename)
                filesInUse.add(filename)

    for f in fileutil.listdir(downloadDir):
        f = os.path.join(downloadDir, f)
        if f not in filesInUse:
            try:
                if fileutil.isfile(f):
                    fileutil.remove (f)
                elif fileutil.isdir(f):
                    fileutil.rmtree (f)
            except OSError:
                # FIXME - maybe a permissions error?
                pass

def kill_uploaders(*args):
    torrent_limit = config.get(prefs.UPSTREAM_TORRENT_LIMIT)
    auto_uploads = list(RemoteDownloader.auto_uploader_view())
    for dler in auto_uploads[torrent_limit:]:
        dler.stopUpload()

def config_change_uploaders(key, value):
    if key == prefs.UPSTREAM_TORRENT_LIMIT.key:
        kill_uploaders()

def limit_uploaders():
    tracker = RemoteDownloader.auto_uploader_view().make_tracker()
    tracker.connect('added', kill_uploaders)
    config.add_change_callback(config_change_uploaders)
    kill_uploaders()

class DownloadDaemonStarter(object):
    def __init__(self):
        RemoteDownloader.initialize_daemon()
        self.downloads_at_startup = list(RemoteDownloader.make_view())
        self.started = False

    def startup(self):
        cleanup_incomplete_downloads()
        RemoteDownloader.dldaemon.start_downloader_daemon()
        limit_uploaders()
        self.restart_downloads()
        self.started = True

    def restart_downloads(self):
        for downloader in self.downloads_at_startup:
            downloader.restartIfNeeded()

    def shutdown(self, callback):
        if not self.started:
            callback()
        else:
            RemoteDownloader.dldaemon.shutdown_downloader_daemon(callback=callback)

def init_controller():
    """Intializes the download daemon controller.

    This doesn't actually start up the downloader daemon, that's done in
    startup_downloader.  Commands will be queued until then.
    """
    global daemon_starter
    daemon_starter = DownloadDaemonStarter()

def startup_downloader():
    """Initialize the downloaders.

    This method currently does 2 things.  It deletes any stale files self in
    Incomplete Downloads, then it restarts downloads that have been restored
    from the database.  It must be called before any RemoteDownloader objects
    get created.
    """
    daemon_starter.startup()

def shutdown_downloader(callback=None):
    if daemon_starter:
        daemon_starter.shutdown(callback)
    elif callback:
        callback()

def lookup_downloader(url):
    try:
        return RemoteDownloader.get_by_url(url)
    except ObjectNotFoundError:
        return None

def get_existing_downloader_by_url(url):
    downloader = lookup_downloader(url)
    return downloader

def get_existing_downloader(item):
    try:
        return RemoteDownloader.get_by_id(item.downloader_id)
    except ObjectNotFoundError:
        return None

def get_downloader_for_item(item):
    existing = get_existing_downloader(item)
    if existing:
        return existing
    url = item.get_url()
    existing = get_existing_downloader_by_url(url)
    if existing:
        return existing
    channelName = unicodeToFilename(item.get_channel_title(True))
    if not channelName:
        channelName = None
    if url.startswith(u'file://'):
        path = getFileURLPath(url)
        try:
            get_torrent_info_hash(path)
        except ValueError:
            raise ValueError("Don't know how to handle %s" % url)
        except IOError:
            return None
        else:
            return RemoteDownloader(url, item, u'application/x-bittorrent', channelName=channelName)
    else:
        return RemoteDownloader(url, item, channelName=channelName)
