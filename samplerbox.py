#
#  SamplerBox
#
#  author:    Joseph Ernest (twitter: @JosephErnest, mail: contact@samplerbox.org)
#  url:       http://www.samplerbox.org/
#  license:   Creative Commons ShareAlike 3.0 (http://creativecommons.org/licenses/by-sa/3.0/)
#
#  samplerbox.py: Main file
#


#########################################
# LOCAL
# CONFIG
#########################################

AUDIO_DEVICE_ID = 1                     # change this number to use another soundcard
SAMPLES_DIR = "."                       # The root directory containing the sample-sets. Example: "/media/" to look for samples on a USB stick / SD card
USE_SERIALPORT_MIDI = False             # Set to True to enable MIDI IN via SerialPort (e.g. RaspberryPi's GPIO UART pins)
USE_PP_MIDI = True                      # Set to True to enable MIDI commands via Scene_Board device connected in USB port (message: "@MIDI:<Status>,<Data0>,<Data1>")
USE_CONSOLE_MIDI = False
USE_I2C_7SEGMENTDISPLAY = False         # Set to True to use a 7-segment display via I2C
USE_BUTTONS = False                     # Set to True to use momentary buttons (connected to RaspberryPi's GPIO pins) to change preset
MAX_POLYPHONY = 80                      # This can be set higher, but 80 is a safe value


#########################################
# IMPORT
# MODULES
#########################################

import wave
import time
import numpy
import os
import re
import sounddevice
import threading
from chunk import Chunk
import struct
import rtmidi_python as rtmidi
import samplerbox_audio


#########################################
# SLIGHT MODIFICATION OF PYTHON'S WAVE MODULE
# TO READ CUE MARKERS & LOOP MARKERS
#########################################

class waveread(wave.Wave_read):

    def initfp(self, file):
        self._convert = None
        self._soundpos = 0
        self._cue = []
        self._loops = []
        self._ieee = False
        self._file = Chunk(file, bigendian=0)
        if self._file.getname() != 'RIFF':
            raise Error, 'file does not start with RIFF id'
        if self._file.read(4) != 'WAVE':
            raise Error, 'not a WAVE file'
        self._fmt_chunk_read = 0
        self._data_chunk = None
        while 1:
            self._data_seek_needed = 1
            try:
                chunk = Chunk(self._file, bigendian=0)
            except EOFError:
                break
            chunkname = chunk.getname()
            if chunkname == 'fmt ':
                self._read_fmt_chunk(chunk)
                self._fmt_chunk_read = 1
            elif chunkname == 'data':
                if not self._fmt_chunk_read:
                    raise Error, 'data chunk before fmt chunk'
                self._data_chunk = chunk
                self._nframes = chunk.chunksize // self._framesize
                self._data_seek_needed = 0
            elif chunkname == 'cue ':
                numcue = struct.unpack('<i', chunk.read(4))[0]
                for i in range(numcue):
                    id, position, datachunkid, chunkstart, blockstart, sampleoffset = struct.unpack('<iiiiii', chunk.read(24))
                    self._cue.append(sampleoffset)
            elif chunkname == 'smpl':
                manuf, prod, sampleperiod, midiunitynote, midipitchfraction, smptefmt, smpteoffs, numsampleloops, samplerdata = struct.unpack(
                    '<iiiiiiiii', chunk.read(36))
                for i in range(numsampleloops):
                    cuepointid, type, start, end, fraction, playcount = struct.unpack('<iiiiii', chunk.read(24))
                    self._loops.append([start, end])
            chunk.skip()
        if not self._fmt_chunk_read or not self._data_chunk:
            raise Error, 'fmt chunk and/or data chunk missing'

    def getmarkers(self):
        return self._cue

    def getloops(self):
        return self._loops


#########################################
# MIXER CLASSES
#
#########################################

class PlayingSound:

    def __init__(self, sound, note):
        self.sound = sound
        self.pos = 0
        self.fadeoutpos = 0
        self.isfadeout = False
        self.note = note

    def fadeout(self, i):
        self.isfadeout = True

    def stop(self):
        try:
            playingsounds.remove(self)
        except:
            pass


class Sound:

    def __init__(self, filename, midinote, velocity):
        wf = waveread(filename)
        self.fname = filename
        self.midinote = midinote
        self.velocity = velocity
        if wf.getloops():
            self.loop = wf.getloops()[0][0]
            self.nframes = wf.getloops()[0][1] + 2
        else:
            self.loop = -1
            self.nframes = wf.getnframes()
        #print("fname:%s,midinote:%d,velocity:%d,loop:%d,nframes:%d"%(self.fname,self.midinote,self.velocity,self.loop,self.nframes))
        self.data = self.frames2array(wf.readframes(self.nframes), wf.getsampwidth(), wf.getnchannels())

        wf.close()

    def play(self, note):
        snd = PlayingSound(self, note)
        playingsounds.append(snd)
        return snd

    def frames2array(self, data, sampwidth, numchan):
        if sampwidth == 2:
            npdata = numpy.fromstring(data, dtype=numpy.int16)
        elif sampwidth == 3:
            npdata = samplerbox_audio.binary24_to_int16(data, len(data)/3)
        if numchan == 1:
            npdata = numpy.repeat(npdata, 2)
        return npdata

FADEOUTLENGTH = 30000
FADEOUT = numpy.linspace(1., 0., FADEOUTLENGTH)            # by default, float64
FADEOUT = numpy.power(FADEOUT, 6)
FADEOUT = numpy.append(FADEOUT, numpy.zeros(FADEOUTLENGTH, numpy.float32)).astype(numpy.float32)
SPEED = numpy.power(2, numpy.arange(0.0, 84.0)/12).astype(numpy.float32)

samples = {}
playingnotes = {}
sustainplayingnotes = []
sustain = False
playingsounds = []
globalvolume = 10 ** (-12.0/20)  # -12dB default global volume
globaltranspose = 0


#########################################
# AUDIO AND MIDI CALLBACKS
#
#########################################

def AudioCallback(outdata, frame_count, time_info, status):
    global playingsounds
    rmlist = []
    playingsounds = playingsounds[-MAX_POLYPHONY:]
    b = samplerbox_audio.mixaudiobuffers(playingsounds, rmlist, frame_count, FADEOUT, FADEOUTLENGTH, SPEED)
    for e in rmlist:
        try:
            playingsounds.remove(e)
        except:
            pass
    b *= globalvolume
    outdata[:] = b.reshape(outdata.shape)

def MidiCallback(message, time_stamp):
    global playingnotes, sustain, sustainplayingnotes
    global preset
    messagetype = message[0] >> 4
    messagechannel = (message[0] & 15) + 1
    note = message[1] if len(message) > 1 else None
    midinote = note
    velocity = message[2] if len(message) > 2 else None
    #print("messagetype:%d,messagechannel:%d,midinote:%d,velocity:%d"%(messagetype,messagechannel,midinote,velocity))
    if messagetype == 9 and velocity == 0:
        messagetype = 8

    if messagetype == 9:    # Note on
        midinote += globaltranspose
        try:
            playingnotes.setdefault(midinote, []).append(samples[midinote, velocity].play(midinote))
        except:
            pass

    elif messagetype == 8:  # Note off
        midinote += globaltranspose
        if midinote in playingnotes:
            for n in playingnotes[midinote]:
                if sustain:
                    sustainplayingnotes.append(n)
                else:
                    n.fadeout(50)
            playingnotes[midinote] = []

    elif messagetype == 12:  # Program change
        print 'Program change ' + str(note)
        preset = note
        LoadSamples()

    elif (messagetype == 11) and (note == 64) and (velocity < 64):  # sustain pedal off
        for n in sustainplayingnotes:
            n.fadeout(50)
        sustainplayingnotes = []
        sustain = False

    elif (messagetype == 11) and (note == 64) and (velocity >= 64):  # sustain pedal on
        sustain = True


#########################################
# LOAD SAMPLES
#
#########################################

LoadingThread = None
LoadingInterrupt = False


def LoadSamples():
    global LoadingThread
    global LoadingInterrupt

    if LoadingThread:
        LoadingInterrupt = True
        LoadingThread.join()
        LoadingThread = None

    LoadingInterrupt = False
    LoadingThread = threading.Thread(target=ActuallyLoad)
    LoadingThread.daemon = True
    LoadingThread.start()

NOTES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]


def ActuallyLoad():
    global preset
    global samples
    global playingsounds
    global globalvolume, globaltranspose
    playingsounds = []
    samples = {}
    globalvolume = 10 ** (-12.0/20)  # -12dB default global volume
    globaltranspose = 0

    samplesdir = SAMPLES_DIR if os.listdir(SAMPLES_DIR) else '.'      # use current folder (containing 0 Saw) if no user media containing samples has been found

    basename = next((f for f in os.listdir(samplesdir) if f.startswith("%d " % preset)), None)      # or next(glob.iglob("blah*"), None)
    if basename:
        dirname = os.path.join(samplesdir, basename)
    if not basename:
        print 'Preset empty: %s' % preset
        display("E%03d" % preset)
        return
    print 'Preset loading: %s (%s)' % (preset, basename)
    display("L%03d" % preset)

    definitionfname = os.path.join(dirname, "definition.txt")
    if os.path.isfile(definitionfname):
        with open(definitionfname, 'r') as definitionfile:
            for i, pattern in enumerate(definitionfile):
                try:
                    if r'%%volume' in pattern:        # %%paramaters are global parameters
                        globalvolume *= 10 ** (float(pattern.split('=')[1].strip()) / 20)
                        continue
                    if r'%%transpose' in pattern:
                        globaltranspose = int(pattern.split('=')[1].strip())
                        continue
                    defaultparams = {'midinote': '0', 'velocity': '127', 'notename': ''}
                    if len(pattern.split(',')) > 1:
                        defaultparams.update(dict([item.split('=') for item in pattern.split(',', 1)[1].replace(' ', '').replace('%', '').split(',')]))
                    pattern = pattern.split(',')[0]
                    pattern = re.escape(pattern.strip())
                    pattern = pattern.replace(r"\%midinote", r"(?P<midinote>\d+)").replace(r"\%velocity", r"(?P<velocity>\d+)")\
                                     .replace(r"\%notename", r"(?P<notename>[A-Ga-g]#?[0-9])").replace(r"\*", r".*?").strip()    # .*? => non greedy
                    for fname in os.listdir(dirname):
                        if LoadingInterrupt:
                            return
                        m = re.match(pattern, fname)
                        if m:
                            info = m.groupdict()
                            midinote = int(info.get('midinote', defaultparams['midinote']))
                            velocity = int(info.get('velocity', defaultparams['velocity']))
                            notename = info.get('notename', defaultparams['notename'])
                            if notename:
                                midinote = NOTES.index(notename[:-1].lower()) + (int(notename[-1])+2) * 12
                            samples[midinote, velocity] = Sound(os.path.join(dirname, fname), midinote, velocity)
                except:
                    print "Error in definition file, skipping line %s." % (i+1)

    else:
        for midinote in range(0, 127):
            if LoadingInterrupt:
                return
            file = os.path.join(dirname, "%d.wav" % midinote)
            if os.path.isfile(file):
                samples[midinote, 127] = Sound(file, midinote, 127)

    initial_keys = set(samples.keys())
    # for midinote in xrange(128):
    #     lastvelocity = None
    #     for velocity in xrange(128):
    #         if (midinote, velocity) not in initial_keys:
    #             samples[midinote, velocity] = lastvelocity
    #         else:
    #             if not lastvelocity:
    #                 for v in xrange(velocity):
    #                     samples[midinote, v] = samples[midinote, velocity]
    #             lastvelocity = samples[midinote, velocity]
    #    if not lastvelocity:
    #         for velocity in xrange(128):
    #             try:
    #                 samples[midinote, velocity] = samples[midinote-1, velocity]
    #             except:
    #                 pass
    if len(initial_keys) > 0:
        print 'Preset loaded: ' + str(preset)
        display("%04d" % preset)
    else:
        print 'Preset empty: ' + str(preset)
        display("E%03d" % preset)


#########################################
# OPEN AUDIO DEVICE
#
#########################################

try:
    sd = sounddevice.OutputStream(device=AUDIO_DEVICE_ID, blocksize=512, samplerate=44100, channels=2, dtype='int16', callback=AudioCallback)
    sd.start()
    print 'Opened audio device #%i' % AUDIO_DEVICE_ID
except:
    print 'Invalid audio device #%i' % AUDIO_DEVICE_ID
    exit(1)


#########################################
# BUTTONS THREAD (RASPBERRY PI GPIO)
#
#########################################

if USE_BUTTONS:
    import RPi.GPIO as GPIO

    lastbuttontime = 0

    def Buttons():
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(18, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        global preset, lastbuttontime
        while True:
            now = time.time()
            if not GPIO.input(18) and (now - lastbuttontime) > 0.2:
                lastbuttontime = now
                preset -= 1
                if preset < 0:
                    preset = 127
                LoadSamples()

            elif not GPIO.input(17) and (now - lastbuttontime) > 0.2:
                lastbuttontime = now
                preset += 1
                if preset > 127:
                    preset = 0
                LoadSamples()

            time.sleep(0.020)

    ButtonsThread = threading.Thread(target=Buttons)
    ButtonsThread.daemon = True
    ButtonsThread.start()


#########################################
# 7-SEGMENT DISPLAY
#
#########################################

if USE_I2C_7SEGMENTDISPLAY:
    import smbus

    bus = smbus.SMBus(1)     # using I2C

    def display(s):
        for k in '\x76\x79\x00' + s:     # position cursor at 0
            try:
                bus.write_byte(0x71, ord(k))
            except:
                try:
                    bus.write_byte(0x71, ord(k))
                except:
                    pass
            time.sleep(0.002)

    display('----')
    time.sleep(0.5)

else:

    def display(s):
        pass


#########################################
# MIDI IN via SERIAL PORT
#
#########################################

if USE_SERIALPORT_MIDI:
    import serial

    ser = serial.Serial('/dev/ttyAMA0', baudrate=38400)       # see hack in /boot/cmline.txt : 38400 is 31250 baud for MIDI!

    def MidiSerialCallback():
        message = [0, 0, 0]
        while True:
            i = 0
            while i < 3:
                data = ord(ser.read(1))  # read a byte
                if data >> 7 != 0:
                    i = 0      # status byte!   this is the beginning of a midi message: http://www.midi.org/techspecs/midimessages.php
                message[i] = data
                i += 1
                if i == 2 and message[0] >> 4 == 12:  # program change: don't wait for a third byte: it has only 2 bytes
                    message[2] = 0
                    i = 3
            MidiCallback(message, None)

    MidiThread = threading.Thread(target=MidiSerialCallback)
    MidiThread.daemon = True
    MidiThread.start()

#########################################
# MIDI IN via USB PORT
#
#########################################

if USE_PP_MIDI:
    import serial

    def MidiPPCallback():
        CONNECT=0
        RESUME=1
        state=CONNECT
        while True:
            if state==CONNECT:
                for dev in os.listdir('/dev'):
                    if dev.startswith('ttyUSB'):
                        print("Connecting to %s" % dev)
                        try:
                            ser = serial.Serial(os.path.join('/dev', dev), baudrate=115200)
                            print("Success")
                            state=RESUME
                        except:
                            print("Fail")
                            pass
                time.sleep(2)
            if state==RESUME:
                message = [0, 0, 0]
                try:
                    line = ser.read_until('\r\n')  # read a line. Expected something like "@MIDI:244,100,127"
                except:
                    print("Lost connection")
                    state=CONNECT
                if line.startswith("@MIDI:"):
                    line=line.replace('@MIDI:', '')
                    line=line.replace('\r\n', '')
                    messageS=line.split(",")
                    success=1
                    for idx in range(3):
                        try:
                            message[idx]=int(messageS[idx])
                        except:
                            success=0
                            pass
                    if success!=0:
                        MidiCallback(message, None)
            
    MidiThread = threading.Thread(target=MidiPPCallback)
    MidiThread.daemon = True
    MidiThread.start()

#########################################
# MIDI IN via Console
#
#########################################

if USE_CONSOLE_MIDI:
    import sys

    def MidiConsoleCallback():
        time.sleep(5)
        message = [0, 0, 0]
        while True:
            line = sys.stdin.readline()  # read a line. Expected something like "@MIDI:244,100,127"
            if line.startswith("@MIDI:"):
                line=line.replace('@MIDI:', '')
                line=line.replace('\r', '')
                line=line.replace('\n', '')
                messageS=line.split(",")
                success=1
                for idx in range(3):
                    try:
                        message[idx]=int(messageS[idx])
                    except:
                        success=0
                        pass
                if success!=0:
                    MidiCallback(message, None)
            
    MidiThread = threading.Thread(target=MidiConsoleCallback)
    MidiThread.daemon = True
    MidiThread.start()

#########################################
# LOAD FIRST SOUNDBANK
#
#########################################

preset = 1
LoadSamples()

#########################################
# MIDI DEVICES DETECTION
# MAIN LOOP
#########################################

midi_in = [rtmidi.MidiIn()]
previous = []
while True:
    for port in midi_in[0].ports:
        if port not in previous and 'Midi Through' not in port:
            midi_in.append(rtmidi.MidiIn())
            midi_in[-1].callback = MidiCallback
            midi_in[-1].open_port(port)
            print 'Opened MIDI: ' + port
    previous = midi_in[0].ports
    time.sleep(2)
