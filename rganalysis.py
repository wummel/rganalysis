#!/usr/bin/env python
# Author: Ryan Thompson

# The Analysis class is modified from code found elsewhere. See the
# notice attached to that class. The Property function was found
# somewhere on the internet. The rest of the code is mine. Since the
# Analysis class is GPL2, then so is this file.

# This program is free software; you can redistribute it and/or modify
# it under the terms of version 2 (or later) of the GNU General Public
# License as published by the Free Software Foundation.

__version__ = '0.2'
import logging
import argparse
import sys
import os
import re
from os.path import realpath, normpath, normcase
import math
import signal
import datetime
import time
import pickle

# Initialize the quodlibet config so tag editing will work correctly
import quodlibet.config
quodlibet.config.init()
from quodlibet.formats import MusicFile
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
# for debugging
#Gst.debug_set_active(True)
#Gst.debug_set_default_threshold(3)
if not Gst.is_initialized():
    Gst.init(None)

import multiprocessing
from multiprocessing.pool import Pool

def default_job_count():
    """Get default job count which is the number of CPU cores, or 1
    if no CPU core count is available.
    """
    try:
        return multiprocessing.cpu_count()
    except Exception:
        return 1


def norm(filename):
    """Norm case and path of given filename."""
    return normcase(normpath(realpath(filename)))


def decode_filename(f):
    """Decode filename with filesystem encoding."""
    if isinstance(f, str):
        f = f.decode(sys.getfilesystemencoding())
    return f


def get_config_dir():
    """Get configuration directory."""
    confdir = os.environ.get("XDG_CONFIG_HOME")
    if not confdir:
        confdir = os.path.expanduser("~")
        if confdir == "~":
            confdir = os.curdir()
        confdir = os.path.join(confdir, ".config")
    return os.path.join(confdir, "rganalysis")


def get_last_runtime_file():
    """Get filename of last runtime file."""
    confdir = get_config_dir()
    return os.path.join(confdir, "lastrun.dict")


def get_last_runtime_dict():
    """Load runtime cache."""
    last_runtime_file = get_last_runtime_file()
    if os.path.isfile(last_runtime_file):
        try:
            with open(last_runtime_file, "rb") as f:
                return pickle.load(f)
        except Exception, msg:
            logging.info("No last runtime dict at %s: %s", last_runtime_file, msg)
    return {}


def write_last_runtime_dict(runtime_dict):
    """Write dictionary with last runtime data."""
    confdir = get_config_dir()
    if not os.path.isdir(confdir):
        try:
            os.makedirs(confdir)
        except OSError as exc: # Python >2.5
            logging.warn("Could not create configuration directory `%s': %s", confdir, exc)
            return
    cur_runtime_dict = get_last_runtime_dict
    last_runtime_file = get_last_runtime_file()
    with open(last_runtime_file, "wb") as f:
        pickle.dump(runtime_dict, f, pickle.HIGHEST_PROTOCOL)
    logging.debug("Wrote last runtime dict at %s", last_runtime_file)


def get_last_runtime_key(music_paths):
    """Return canonical key for given music paths."""
    return "|".join(sorted(music_paths))


def get_mtime(filename):
    """Return modification time of filename or zero on errors."""
    try:
        return os.path.getmtime(filename)
    except os.error:
        return 0


def format_time(timestamp):
    """Return human readable time representation of given timestamp in seconds."""
    dt = datetime.datetime.fromtimestamp(timestamp)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def Property(function):
    keys = 'fget', 'fset', 'fdel'
    func_locals = {'doc':function.__doc__}
    def probe_func(frame, event, arg):
        if event == 'return':
            locals = frame.f_locals
            func_locals.update(dict((k,locals.get(k)) for k in keys))
            sys.settrace(None)
        return probe_func
    sys.settrace(probe_func)
    function()
    return property(**func_locals)

class Analyzer(object):
    """Analyse replaygain of a list of files.
    All files must be readable, regular files and be decodeable by GStreamer.
    """
    # The following class is taken from code bearing the following
    # copyright notice. It was found at this URL:
    # http://www.tortall.net/mu/wiki/rganalysis.py

    #    Copyright (C) 2007  Michael Urman
    #
    #    This program is free software; you can redistribute it and/or modify
    #    it under the terms of version 2 of the GNU General Public License as
    #    published by the Free Software Foundation.
    #
    # And from http://www.bytebucket.org/fk/rgain/src
    def __init__(self, files, reference_level=89):
        # This is a long-running script, so some files can change
        # while it's running. Therefore, skip files that have become
        # inaccessible.
        files = [ f for f in files if os.access(f, os.R_OK) ]

        # build gstreamer pipe:
        # filesrc -> decodebin -> audioconvert -> audioresample -> rganalysis -> fakesink
        self.filesrc = Gst.ElementFactory.make("filesrc", "source")
        self.decode = Gst.ElementFactory.make("decodebin", "decode")
        self.convert = Gst.ElementFactory.make("audioconvert", "convert")
        self.resample = Gst.ElementFactory.make("audioresample", "resample")
        self.analysis = Gst.ElementFactory.make("rganalysis", "analysis")
        self.analysis.set_property("num-tracks", len(files))
        self.analysis.set_property("forced", True)
        self.analysis.set_property("reference-level", reference_level)
        self.sink = Gst.ElementFactory.make("fakesink", "sink")

        self.pipe = Gst.Pipeline()
        self.pipe.add(self.filesrc)
        self.pipe.add(self.decode)
        self.pipe.add(self.convert)
        self.pipe.add(self.resample)
        self.pipe.add(self.analysis)
        self.pipe.add(self.sink)

        self.filesrc.link(self.decode)
        # the decodebin generates dynamic link sources ("pads")
        # which are connected and subsequently removed by the
        # new_decoded_pad() method
        self.decode.connect('pad-added', self.new_decoded_pad)
        self.decode.connect('pad-removed', self.removed_decoded_pad)
        self.convert.link(self.resample)
        self.resample.link(self.analysis)
        self.analysis.link(self.sink)

        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::tag", self.bus_message_tag)

        self.data = {
            'track_gain': {},
            'track_peak': {},
            'album_gain': None,
            'album_peak': None
        }

        for f in files:
            self.current_song = f
            logging.info('Analyzing %r', os.path.basename(f))
            self.filesrc.set_property("location", realpath(f))
            res = self.pipe.set_state(Gst.State.PLAYING)
            if res == Gst.StateChangeReturn.ASYNC:
                # wait for asynchronous state change to finish
                res = self.pipe.get_state(Gst.CLOCK_TIME_NONE)[0]
            assert res == Gst.StateChangeReturn.SUCCESS

            while True:
                message = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE,
                    Gst.MessageType.TAG|Gst.MessageType.EOS|Gst.MessageType.ERROR)
                #logging.debug("message %s", message.type)
                if message.type == Gst.MessageType.TAG:
                    self.bus_message_tag(message)
                elif message.type == Gst.MessageType.EOS:
                    self.analysis.set_locked_state(True)
                    res = self.pipe.set_state(Gst.State.NULL)
                    assert res == Gst.StateChangeReturn.SUCCESS
                    # For some reason, GStreamer 1.0 hangs on setting the PLAYING
                    # state above after processing the first file.
                    # It does not hang when a flush has been performed. Gah.
                    # In other words: why does gstreamer suck so much?
                    # Major thanks to the python-rgain author(s) for finding this
                    # workaround.
                    pad = self.analysis.get_static_pad("src")
                    pad.send_event(Gst.Event.new_flush_start())
                    pad.send_event(Gst.Event.new_flush_stop(True))
                    self.analysis.set_locked_state(False)
                    break
                elif message.type == Gst.MessageType.ERROR:
                    raise Exception("Gst stream error")
        logging.info("Finished analysis of %s tracks." % len(files))

    def new_decoded_pad(self, decodebin, pad):
        logging.debug("link pad %s", pad.get_name())
        pad.link(self.convert.get_static_pad("sink"))

    def removed_decoded_pad(self, decodebin, pad):
        logging.debug("unlink pad %s", pad.get_name())
        pad.unlink(self.convert.get_static_pad("sink"))

    def bus_message_tag(self, message):
        tags = message.parse_tag()
        copied, value = tags.get_double(Gst.TAG_TRACK_PEAK)
        if copied:
            # track peak tag did exist
            logging.debug("set track peak %s", value)
            self.data['track_peak'][self.current_song] = '%.4f' % value
        copied, value = tags.get_double(Gst.TAG_TRACK_GAIN)
        if copied:
            # track gain tag did exist
            logging.debug("set track gain %s", value)
            self.data['track_gain'][self.current_song] = '%.2f dB' % value
        copied, value = tags.get_double(Gst.TAG_ALBUM_PEAK)
        if copied:
            # album peak tag did exist
            logging.debug("set album peak %s", value)
            self.data['album_peak'] = '%.4f' % value
        copied, value = tags.get_double(Gst.TAG_ALBUM_GAIN)
        if copied:
            # album gain tag did exist
            logging.debug("set album gain %s", value)
            self.data['album_gain'] = '%.2f dB' % value

class RGTrackSet(object):
    '''Represents an album and supplies methods to analyze the tracks in that
       album for replaygain information, as well as store that information in the tracks.'''

    def __init__(self, tracks):
        self.RGTracks = dict((t.filename, t) for t in tracks)
        if len(self.RGTracks) < 1:
            raise ValueError("Need at least one track to analyze")
        self.changed = False
        keys = set(t.track_set_key for t in self.RGTracks.values())
        if (len(keys) != 1):
            raise ValueError("All tracks in an album must have the same key")
        if self.has_valid_rgdata():
            self.analyzed = True
        else:
            self.analyzed = False
        self.errors = 0

    def __repr__(self):
        return "RGTrackSet(%s)" % repr(self.RGTracks.values())

    @classmethod
    def MakeTrackSets(cls, tracks):
        '''Takes a list of RGTrack objects and returns a
        list of RGTrackSet objects, one for each track_set_key represented in
        the RGTrack objects.'''
        track_sets = {}
        for t in tracks:
            track_sets.setdefault(t.track_set_key, []).append(t)
        return [cls(x) for x in track_sets.values()]

    def want_album_gain(self):
        '''Return true if this track set should have album gain tags,
        or false if not.'''
        return len(self.RGTracks) > 1 and self.track_set_key is not None

    @Property
    def gain():
        doc = "Album gain value, or None if tracks do not all agree on it."
        tag = 'replaygain_album_gain'
        def fget(self):
            return(self._get_tag(tag))
        def fset(self, value):
            self._set_tag(tag, value)
        def fdel(self):
            self._del_tag(tag)

    @Property
    def peak():
        doc = "Album peak value, or None if tracks do not all agree on it."
        tag = 'replaygain_album_peak'
        def fget(self):
            return(self._get_tag(tag))
        def fset(self, value):
            self._set_tag(tag, value)
        def fdel(self):
            self._del_tag(tag)

    @Property
    def filenames():
        def fget(self):
            return sorted(self.RGTracks.keys())

    @Property
    def num_tracks():
        def fget(self):
            return len(self.RGTracks)

    @Property
    def length_seconds():
        def fget(self):
            return sum(t.length_seconds for t in self.RGTracks.itervalues())

    @Property
    def track_set_key():
        def fget(self):
            return next(self.RGTracks.itervalues()).track_set_key

    def __unicode__(self):
        return unicode(next(self.RGTracks.itervalues()))

    def __str__(self):
        return unicode(self).encode("iso-8859-1", "replace")

    @Property
    def directory():
        def fget(self):
            return self.track_set_key[1]

    def log_exception(self, msg):
        logging.exception("%s %s", msg, self)
        self.errors += 1

    def __len__(self):
        return self.length_seconds

    def _get_tag(self, tag):
        '''Get the value of a tag, only if all tracks in the album
        have the same value for that tag. If the tracks disagree on
        the value, return False. If any of the tracks is missing the
        value entirely, return None.

        In particular, note that None and False have different
        meanings.'''
        try:
            tags = set(t.track[tag] for t in self.RGTracks.itervalues())
            if len(tags) == 1:
                return tags.pop()
            elif len(tags) > 1:
                return False
            else:
                return None
        except KeyError:
            return None

    def _set_tag(self, tag, value):
        '''Set tag to value in all tracks in the album.'''
        logging.debug("Setting %s to %s in all tracks in %s.", tag, value, self)
        for t in self.RGTracks.itervalues():
            t.track[tag] = str(value)

    def _del_tag(self, tag):
        '''Delete tag from all tracks in the album.'''
        logging.debug("Deleting %s in all tracks in %s.", tag, self)
        for t in self.RGTracks.itervalues():
            try:
                del t.track[tag]
            except KeyError: pass

    def analyze(self, force=False):
        """Analyze all tracks in the album, and add replay gain tags
        to the tracks based on the analysis.

        If force is False (the default) and the album already has
        replay gain tags, then do nothing.
        """
        if force:
            self.analyzed = False

        if self.analyzed:
            logging.info('Skipping track set "%s", which is already analyzed.', self)
        else:
            logging.info('Analyzing %d tracks of "%s"', self.num_tracks, self)
            rgdata = Analyzer(self.filenames).data
            # Only want album gain for real albums, not single tracks
            if self.want_album_gain():
                self.gain = rgdata['album_gain']
                self.peak = rgdata['album_peak']
            else:
                del self.gain
                del self.peak
            for filename in self.filenames:
                rgtrack = self.RGTracks[filename]
                rgtrack.gain = rgdata['track_gain'][filename]
                rgtrack.peak = rgdata['track_peak'][filename]
            self.changed = True
            self.analyzed = True

    def has_valid_rgdata(self):
        """Returns true if the album's replay gain data appears valid.
        This means that all tracks have replay gain data, and all
        tracks have the *same* album gain data (it want_album_gain is True).

        If the album has only one track, or if this album is actually
        a collection of albumless songs, then only track gain data is
        checked."""
        # Make sure every track has valid gain data
        for t in self.RGTracks.itervalues():
            if not t.has_valid_rgdata():
                return False
        # For "real" albums, check the album gain data
        if self.want_album_gain():
            # These will only be non-null if all tracks agree on their
            # values. See _get_tag.
            if self.gain and self.peak:
                return True
            elif self.gain is None or self.peak is None:
                return False
            else:
                return False
        else:
            if self.gain is not None or self.peak is not None:
                return False
            else:
                return True

    def report(self):
        """Report calculated replay gain tags."""
        for k in sorted(self.filenames):
            track = self.RGTracks[k]
            logging.info("Set track gain tags for %s:\n\tTrack Gain: %s\n\tTrack Peak: %s", track.filename, track.gain, track.peak)
        if self.want_album_gain():
            logging.info("Set album gain tags for %s:\n\tAlbum Gain: %s\n\tAlbum Peak: %s", self, self.gain, self.peak)
        else:
            logging.info("Did not set album gain tags for %s.", self)

    def save(self):
        """Save the calculated replaygain tags"""
        if not self.analyzed:
            raise Exception('Track set "%s" must be analyzed before saving' % self)
        self.report()
        if self.changed:
            for k in self.filenames:
                track = self.RGTracks[k]
                track.save()
            self.changed = False


def has_track_gain_signal_filename(directory):
    """Check if directory should have no album gain tags."""
    return any(os.path.exists(os.path.join(directory, f))
        for f in ('TRACKGAIN', '.TRACKGAIN', '_TRACKGAIN'))


class RGTrack(object):
    '''Represents a single track along with methods for analyzing it
    for replaygain information.'''

    def __init__(self, track, gain_type, dry_run=False):
        self.track = track
        directory = os.path.dirname(self.filename)
        if gain_type == "album":
            self.track_set_key = (directory, track.album_key, type(track))
        elif gain_type == "folder":
            self.track_set_key = directory
        elif gain_type == "track":
            self.track_set_key = None
        elif gain_type == "auto":
            # Check for track gain signal files
            if has_track_gain_signal_filename(directory):
                self.track_set_key = None
            else:
                self.track_set_key = (directory, track.album_key, type(track))
        else:
            raise ValueError("invalid gain_type %r" % gain_type)
        self.dry_run = dry_run

    def __repr__(self):
        return "RGTrack(%s)" % (repr(self.track), )

    def has_valid_rgdata(self):
        '''Returns True if the track has valid replay gain tags. The
        tags are not checked for accuracy, only existence.'''
        return self.gain and self.peak

    @Property
    def filename():
        def fget(self):
            return decode_filename(self.track['~filename'])
        def fset(self, value):
            self.track['~filename'] = value

    def __unicode__(self):
        '''A human-readable string representation of this track.'''
        album = self.track("albumsort", u"")
        directory = os.path.dirname(self.filename)
        filetype = type(self.track)
        if album == u'':
            key_string = u"No album"
        else:
            key_string = album
        key_string += u" in directory %s" % directory
        key_string += u" of type %s" % (re.sub(u"File$",u"",filetype.__name__),)
        return key_string

    def __str__(self):
        return unicode(self).encode("iso-8859-1", "replace")

    @Property
    def gain():
        doc = "Track gain value, or None if the track does not have replaygain tags."
        tag = 'replaygain_track_gain'
        def fget(self):
            try:
                return(self.track[tag])
            except KeyError:
                return None
        def fset(self, value):
            # print "Setting %s to %s for %s" % (tag, value, self.filename)
            self.track[tag] = str(value)
        def fdel(self):
            if self.track.has_key(tag):
                del self.track[tag]

    @Property
    def peak():
        doc = "Track peak dB, or None if the track does not have replaygain tags."
        tag = 'replaygain_track_peak'
        def fget(self):
            try:
                return(self.track[tag])
            except KeyError:
                return None
        def fset(self, value):
            # print "Setting %s to %s for %s" % (tag, value, self.filename)
            self.track[tag] = str(value)
        def fdel(self):
            if self.track.has_key(tag):
                del self.track[tag]

    @Property
    def length_seconds():
        def fget(self):
            return self.track['~#length']

    def __len__(self):
        return self.length_seconds

    def save(self):
        if not self.dry_run:
            self.track.write()


_hidden_path_re = re.compile(r'^\.')
def is_hidden_path(path):
    """Check for Unix-style hidden paths."""
    return _hidden_path_re.search(path)


def get_all_music_files (paths, ignore_hidden=True):
    '''Recursively search in one or more paths for music files.
    By default, hidden files and directories are ignored.'''
    filenames = set()
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p, followlinks=True, topdown=True):
                logging.debug("Walking %s", root)
                if ignore_hidden:
                    files = [f for f in files if not is_hidden_path(f)]
                    for dirname in dirs[:]:
                        if is_hidden_path(dirname):
                            dirs.remove(dirname)
                filenames.update(norm(os.path.join(root, f)) for f in files)
        else:
            filenames.add(norm(p))
    logging.debug("Found %d files", len(filenames))
    # Try to load every file as an audio file, and filter the
    # ones that aren't actually audio files
    for filename in filenames:
        if os.path.isfile(filename):
            logging.debug("Loading %r", filename)
            mf = MusicFile(filename)
            if mf is not None:
                yield mf


def get_max_mtime(paths, ignore_hidden=True):
    """Get maximum modification time for given music paths."""
    mtime = 0
    for p in paths:
         mtime = max(mtime, get_mtime(p))
    logging.debug("Modification time %s", format_time(mtime))
    return mtime


class TrackSetHandler(object):
    """Pickleable stateful callable for multiprocessing.Pool"""
    def __init__(self, force=False):
        self.force = force

    def __call__(self, track_set):
        try:
            track_set.analyze(force=self.force)
        except Exception:
            track_set.log_exception("Failed to analyze")
        else:
            try:
                track_set.save()
            except Exception:
                track_set.log_exception("Failed to save")
        return track_set


def positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
         raise argparse.ArgumentTypeError("%s is an invalid positive int value" % value)
    return ivalue


def main(music_paths, force_reanalyze=False, include_hidden=False,
         dry_run=False, gain_type='auto',
         jobs=default_job_count(),
         quiet=False, verbose=False):
    if quiet:
        level=logging.WARN
    elif verbose:
        level=logging.DEBUG
    else:
        level=logging.INFO
    format = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=level, format=format)

    # Some pesky functions used below will catch KeyboardInterrupts
    # inappropriately, so install an alternate handler that bypasses
    # KeyboardInterrupt instead.
    def signal_handler(sig, frame):
        print "Canceled."
        os.kill(os.getpid(), signal.SIGTERM)
    original_handler = signal.signal(signal.SIGINT, signal_handler)

    if dry_run:
        logging.warn('This script is running in "dry run" mode, so no files will actually be modified.')

    if len(music_paths) == 0:
        logging.error("You did not specify any music files or directories. Exiting.")
        sys.exit(1)
    for music_path in music_paths[:]:
        if not (os.path.isdir(music_path) or os.path.isfile(music_path)):
            logging.warn("Ignoring `%s' since it's not a file or directory.", music_path)
            music_paths.remove(music_path)
    if len(music_paths) == 0:
        logging.error("No valid music files or directories specified. Exiting.")
        sys.exit(1)

    last_runtime_dict = get_last_runtime_dict()
    # for later storage
    now = time.time()
    if not force_reanalyze:
        key = get_last_runtime_key(music_paths)
        last_runtime = last_runtime_dict.get(key)
        if last_runtime:
            max_mtime = get_max_mtime(music_paths, ignore_hidden=(not include_hidden))
            if max_mtime < last_runtime:
                logging.info("Last runtime %s is newer than last modification time %s",
                    format_time(last_runtime), format_time(max_mtime))
                logging.info("Use --force-reanalyze to reanalyze anyway.")
                sys.exit(0)
            logging.info("Last runtime %s is older than last modification time %s",
                format_time(last_runtime), format_time(max_mtime))

    logging.info("Searching for music in the following paths:\n%s", "\n".join(music_paths),)
    files = get_all_music_files(music_paths, ignore_hidden=(not include_hidden))
    tracks = [RGTrack(f, gain_type, dry_run=dry_run) for f in files]

    # Filter out tracks for which we can't get the length
    def is_valid_track(t):
        try:
            len(t)
            return True
        except Exception, msg:
            logging.error("Track %s appears to be invalid: %s. Skipping.", t.filename, msg)
        return False

    tracks = [t for t in tracks if is_valid_track(t)]
    if not tracks:
        logging.error("Failed to find any valid tracks in the paths you specified. Exiting.")
        sys.exit(1)
    track_sets = RGTrackSet.MakeTrackSets(tracks)
    num_track_sets = len(track_sets)
    num_tracks = sum(ts.num_tracks for ts in track_sets)
    logging.info("Found %d track sets with %d tracks total.", num_track_sets, num_tracks)

    # Remove the earlier bypass of KeyboardInterrupt
    signal.signal(signal.SIGINT, original_handler)

    handler = TrackSetHandler(force=force_reanalyze)

    # For display purposes, calculate how much granularity is required
    # to show visible progress at each update
    logging.info("Calculating stats")
    total_length = sum(len(ts) for ts in track_sets)
    min_step = min(len(ts) for ts in track_sets)
    places_past_decimal = max(0,int(math.ceil(-math.log10(min_step * 100.0 / total_length))))
    update_string = '%.' + str(places_past_decimal) + 'f%% done'

    errors = 0
    pool = Pool(jobs)
    try:
        # start parallel analysis
        logging.info("Beginning analysis")
        handled_track_sets = pool.imap_unordered(handler, track_sets)
        processed_length = 0
        percent_done = 0
        logging.info(update_string, percent_done)
        for ts in handled_track_sets:
            processed_length = processed_length + len(ts)
            percent_done = 100.0 * processed_length / total_length
            logging.info(update_string, percent_done)
            errors += ts.errors
        logging.debug("Closing pool")
        pool.close()
        logging.debug("Joining pool")
        pool.join()
    except KeyboardInterrupt:
        logging.debug("Terminating process pool")
        pool.terminate()
        raise
    logging.info("Analysis complete.")
    if dry_run:
        logging.warn('This script ran in "dry run" mode, so no files were actually modified.')
    elif not errors:
        key = get_last_runtime_key(music_paths)
        last_runtime_dict[key] = now
        write_last_runtime_dict(last_runtime_dict)
    return 1 if errors > 0 else 0


def parse_options():
    parser = argparse.ArgumentParser(description='Add replaygain tags to your music files.')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help="Print debug messages that are probably only useful if something is going wrong.")
    parser.add_argument('-q', '--quiet', action='store_true', default=False, help="Do not print informational messages.")
    parser.add_argument('-j', '--jobs', action='store', type=positive_int, default=default_job_count(), help="Number of albums to analyze in parallel. The default is the number of cores detected on your system.")
    parser.add_argument('-n', '--dry-run', action='store_true', default=False, help="Don't modify any files. Only analyze and report gain.")
    parser.add_argument('-g', '--gain-type', action='store', default='auto',
       choices=("album", "track", "folder", "auto"),
       help='Can be "album", "track", "folder" or "auto".'
       ' If "track", only track gain values will be calculated, and album gain values will be erased.'
       ' If "album", both track and album gain values will be calculated for detected albums.'
       ' If "folder", both track and album gain values will be calculated for files in the same folder.'
       ' If "auto", then "album" mode will be used except in directories that contain a file called "TRACKGAIN" or ".TRACKGAIN". In these directories, "track" mode will be used.'
       ' The default setting is "auto".')
    parser.add_argument('-i', '--include-hidden', action='store_true', help='Do not skip hidden files and directories.')
    parser.add_argument('-f', '--force-reanalyze', action='store_true', help='Reanalyze all files and recalculate replaygain values, even if the files already have valid replaygain tags. Normally, only files without replaygain tags will be analyzed.')
    parser.add_argument('--version', action='store_true', help='display the version number')
    parser.add_argument('music_paths', metavar='music_path', nargs='*', help="Music files or directories to search for music tracks.")
    return parser.parse_args()


if __name__=="__main__":
    try:
        options = parse_options()
        if options.version:
            print __version__
            sys.exit(0)
        res = main(options.music_paths,
             force_reanalyze=options.force_reanalyze,
             include_hidden=options.include_hidden,
             dry_run=options.dry_run,
             gain_type=options.gain_type,
             jobs=options.jobs,
             quiet=options.quiet,
             verbose=options.verbose)
        sys.exit(res)
    except KeyboardInterrupt:
        logging.error("Canceled.")
        sys.exit(1)
