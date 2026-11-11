"""Experimental .013/.014 wheel pulses: MCU duration cap, correlated replies, no retry.

On .014 a pulse with a new sequence replaces a running one (same caps and gates),
and read-only diagnostics pages (20 7A) report component versions and radio
update traffic recorded by the main controller.

Only for supervised wheels-clear tests until physical stop/expiry are confirmed.
Values are firmware PWM input units, not calibrated speed or distance.
"""
import asyncio
import secrets
import time
from mtiny_controller import MTinyController
from mtiny_protocol import frame

def pulse_frame(sequence,left,right,duration_ms):
    if type(sequence) is not int or not 0<=sequence<128:
        raise ValueError('Sequence must be a seven-bit integer')
    if any(type(v) is not int or v%10 or abs(v)>200 for v in (left,right)):
        raise ValueError('Wheel input must be a multiple of10 within -200..200')
    if type(duration_ms) is not int or duration_ms%10 or not 0<=duration_ms<=250:
        raise ValueError('Duration must be 0..250ms in10ms increments')
    if duration_ms==0 and (left or right):
        raise ValueError('Zero duration is reserved for stop')
    return frame(1,0,bytes((0x20,0x7c,sequence,64+left//10,64+right//10,duration_ms//10)))

def decode_motor_status(body):
    if len(body)!=13 or body[:1]!=b'\x20' or body[1] not in (0x7b,0x7c):
        raise ValueError('Malformed motor status')
    def signed(units):
        v=sum(b<<(7*i) for i,b in enumerate(units))
        if v>65535:raise ValueError('Invalid signed16 encoding')
        return v-65536 if v&32768 else v
    return dict(sequence=body[2],result=body[3],active=body[4],reason=body[5],
                left=signed(body[6:9]),right=signed(body[9:12]),mode=body[12])

MOTOR_VERSIONS=('28.01.005.013.7100','28.01.005.014.7100','28.01.005.015.7100','28.01.005.016.7100')
DIAGNOSTIC_VERSIONS=MOTOR_VERSIONS[1:]
STEP_VERSIONS=MOTOR_VERSIONS[3:]
# Stock speed-controller step kinds. Turn directions are predicted from emulated
# wheel targets (kind 2: left wheel back, right wheel forward), not yet observed.
STEP_KINDS=dict(forward=0,backward=1,left=2,right=3,wheels=4)
STEP_REASONS=('running','finished','time limit','protection','updater','host silent','stopped')

def step_frame(sequence,kind,a=0,b=0,up=0,down=0,ms=300):
    """.016 `20 79`: one step through the robot's own speed controller.

    a/b: speed and turn offset for forward/backward/left/right, left and right
    wheel speed for wheels; multiples of 5 within -200..200. Forward speed is
    probably mm/s and turn speed degrees/s (inferred from firmware constants).
    up/down: ramps in ms, multiples of 5 up to 635. ms: 20..5000.
    """
    if type(sequence) is not int or not 0<=sequence<128:
        raise ValueError('Sequence must be a seven-bit integer')
    if kind not in STEP_KINDS:
        raise ValueError('Step kind must be one of '+', '.join(STEP_KINDS))
    if any(type(v) is not int or v%5 or abs(v)>200 for v in (a,b)):
        raise ValueError('Step speeds must be multiples of 5 within -200..200')
    if any(type(v) is not int or v%5 or not 0<=v<=635 for v in (up,down)):
        raise ValueError('Ramps must be multiples of 5 within 0..635 ms')
    if type(ms) is not int or not 20<=ms<=5000 or up+down>ms:
        raise ValueError('Step time must be 20..5000 ms and contain both ramps')
    return frame(1,0,bytes((0x20,0x79,sequence,STEP_KINDS[kind],64+a//5,64+b//5,up//5,down//5,ms&127,ms>>7)))

def decode_step_status(body):
    if len(body)!=20 or body[:2]!=b'\x20\x79':
        raise ValueError('Malformed step status')
    def signed(units):
        v=_units(units)&0xffffffff
        return v-(1<<32) if v&(1<<31) else v
    return dict(sequence=body[2],result=body[3],owned=body[4],
                reason=STEP_REASONS[body[5]] if body[5]<len(STEP_REASONS) else body[5],
                mode=body[6],done=body[7],phase=body[8],activity_state=body[9],
                left_encoder=signed(body[10:15]),right_encoder=signed(body[15:20]))
# White-key note clips G3..F5 (pitch measured from the decoded files, 2026-11-11).
TUNE_NOTES={name:f'system/subject/music/0x{0x84D+i:X}.mp3' for i,name in enumerate(
    'G3 A3 B3 C4 D4 E4 F4 G4 A4 B4 C5 D5 E5 F5'.split())}
FORWARD_SCALE=1.07 # measured 2026-11-11: 203 mm travelled for a nominal 189 mm
def plan_moves(steps):
    """Validate a move list and turn friendly items into exact steps.

    Items: dict(kind='forward'|'backward', mm=, speed=100)   distance move
           dict(kind='left'|'right', deg=, speed=180)        turn on the spot
           dict(kind=..., a=, b=, up=, down=, ms=)           raw .016 step
           dict(kind='wait', ms=)  dict(kind='eyes', a=, b=)  dict(kind='sound', path=)
           dict(kind='tune', notes='C4 C4 G4 - ...', beat=450)   one note clip per beat
    A step started from rest ramps for 200 ms whatever is asked, so the cruise
    equivalent of a step is ms - 100 - down/2. Slow down rather than go below 300 ms.
    """
    if not isinstance(steps,list) or not 1<=len(steps)<=32:
        raise ValueError('Give 1..32 steps')
    plan=[]
    def tune(item):
        notes,beat=item.get('notes'),item.get('beat',450)
        if set(item)-{'kind','notes','beat'} or not isinstance(notes,str) or type(beat) is not int or not 150<=beat<=2000:
            raise ValueError('Tune needs notes and a beat of 150..2000 ms')
        words=notes.split()
        if not 1<=len(words)<=64 or any(w!='-' and w not in TUNE_NOTES for w in words):
            raise ValueError('Tune notes are G3..F5 white keys or - for a rest, at most 64')
        plan.append(('tune',tuple(None if w=='-' else TUNE_NOTES[w] for w in words),beat))
    for item in steps:
        if not isinstance(item,dict):raise ValueError('Each step must be an object')
        kind=item.get('kind');rest=set(item)-{'kind'}
        if kind=='tune':tune(item)
        elif kind=='wait':
            if rest-{'ms'} or type(item.get('ms')) is not int or not 10<=item['ms']<=5000:
                raise ValueError('Wait needs ms 10..5000')
            plan.append(('wait',item['ms']))
        elif kind=='eyes':
            a,b=item.get('a',2),item.get('b',item.get('a',2))
            if rest-{'a','b'} or any(type(v) is not int or not 0<=v<=127 for v in (a,b)):
                raise ValueError('Eyes need animation IDs 0..127')
            plan.append(('eyes',a,b))
        elif kind=='sound':
            path=item.get('path')
            if rest-{'path'} or not isinstance(path,str) or not path.startswith('system/') or '..' in path.split('/'):
                raise ValueError('Sound path must name an installed file under system/')
            plan.append(('sound',path))
        elif 'mm' in item or 'deg' in item:
            unit='mm' if kind in ('forward','backward') else 'deg' if kind in ('left','right') else None
            if unit is None or unit not in item or rest-{unit,'speed'}:
                raise ValueError('Use mm with forward/backward and deg with left/right')
            amount=item[unit];speed=item.get('speed',100 if unit=='mm' else 180)
            if any(type(v) is not int for v in (amount,speed)) or amount<1 or not 5<=speed<=200:
                raise ValueError('Distance must be a positive whole number and speed 5..200')
            scale=FORWARD_SCALE if unit=='mm' else 1.0
            down=40;lost=100+down/2
            speed=max(5,min(speed//5*5,int(amount*1000/scale/(300-lost))//5*5))
            ms=round(amount*1000/scale/speed+lost)
            if ms>5000:raise ValueError('Move takes longer than 5 s; raise speed or split it')
            args=(kind,speed,0,0,down,ms);step_frame(0,*args);plan.append(args)
        else:
            if rest-{'a','b','up','down','ms'}:raise ValueError('Unexpected step fields')
            args=(kind,item.get('a',0),item.get('b',0),item.get('up',0),item.get('down',0),item.get('ms',300))
            step_frame(0,*args);plan.append(args)
    return plan

COMPONENTS=('car_radio','pen_radio','pen_main','audio')
UPDATE_STEPS=('sub4','upgrade_request','sub6','file_info_or_data','end','sub9')

def _units(units):
    return sum(b<<(7*i) for i,b in enumerate(units))

def diagnostics_page_frame(sequence,page):
    if type(sequence) is not int or not 0<=sequence<128:
        raise ValueError('Sequence must be a seven-bit integer')
    if type(page) is not int or not 0<=page<=5:
        raise ValueError('Diagnostics page must be 0..5')
    return frame(1,0,bytes((0x20,0x7a,sequence,page)))

def decode_diagnostics_page(body):
    """Decode a .014 `20 7A seq page result ...` reply. Ages and uptime are ms."""
    if len(body)<5 or body[:2]!=b'\x20\x7a' or body[4]:
        raise ValueError('Malformed or refused diagnostics page')
    page,data=body[3],body[5:]
    if page<4:
        if len(data)!=15:raise ValueError('Malformed version page')
        a,b,c,d=data[1:5]
        return dict(component=COMPONENTS[page],replies=data[0],
                    version=f'28.{b:02}.{c:03}.{d:03}' if data[0] and a==28 else None,
                    raw=list(data[1:5]),age_ms=_units(data[5:10]),uptime_ms=_units(data[10:15]))
    if len(data)!=(18 if page==4 else 27):raise ValueError('Malformed update-traffic page')
    counts={name:_units(data[i*3:i*3+3]) for i,name in enumerate(UPDATE_STEPS)}
    if page==4:return dict(direction='to_car_radio',frames=counts)
    return dict(direction='from_car_radio',frames=counts,last_subservice=data[18],
                last_payload=list(data[19:22]),last_age_ms=_units(data[22:27]))

class MTinyMovement(MTinyController):
    def __init__(self,event=None):
        super().__init__(event)
        self.motor_sequence=secrets.randbelow(128)
        self.motor_may_be_active=False
        self.motor_stop_epoch=0
    def _motor_gate(self):
        self._healthy()
        if self.version not in MOTOR_VERSIONS:
            raise RuntimeError('Timed motor pulses require installed .013 or later; no motor request sent')
    def _sequence(self):
        self.motor_sequence=(self.motor_sequence+1)%128
        return self.motor_sequence
    async def _motor_request(self,packet,selector,sequence,priority=False,epoch=None):
        async def request():
            if epoch is not None and epoch!=self.motor_stop_epoch:
                raise RuntimeError('Pulse cancelled by stop before transmission')
            return await self._request(packet,lambda r:r.destination==1 and r.subservice==0
                         and len(r.payload)==13 and r.payload[:3]==bytes((0x20,selector,sequence)))
        # Correlated stop replies can bypass a pending foreground reply. The
        # transport lock still preserves complete-frame ordering and spacing.
        if priority:reply=await request()
        else:
            async with self.operation_lock:reply=await request()
        status=decode_motor_status(reply.payload)
        self.event('motor_status',**status,physical_stop_confirmed=False)
        if status['result']:
            raise RuntimeError(f'Motor request refused: status {status["result"]}')
        return status
    async def motor_status(self):
        self._motor_gate();seq=self._sequence()
        return await self._motor_request(frame(1,0,bytes((0x20,0x7b,seq))),0x7b,seq)
    async def diagnostics_page(self,page):
        """Read one passive record page. Requires .014; sends nothing to the radio."""
        self._healthy()
        if self.version not in DIAGNOSTIC_VERSIONS:
            raise RuntimeError('Component diagnostics require installed .014 or later; no request sent')
        seq=self._sequence();packet=diagnostics_page_frame(seq,page)
        async with self.operation_lock:
            reply=await self._request(packet,lambda r:r.destination==1 and r.subservice==0
                         and r.payload[:4]==bytes((0x20,0x7a,seq,page)))
        result=decode_diagnostics_page(reply.payload)
        self.event('diagnostics_page',page=page,**result)
        return result
    async def component_report(self,ask_radio=False):
        """All six pages. ask_radio first has the main controller query the car radio's
        version (stock selector 3); that request has no direct reply."""
        if ask_radio:
            self._healthy()
            async with self.operation_lock:
                await self._write(frame(1,0,bytes((0x20,3))))
            await asyncio.sleep(.5)
        pages=[await self.diagnostics_page(p) for p in range(6)]
        return dict(versions={p['component']:p for p in pages[:4]},
                    update_to_radio=pages[4],update_from_radio=pages[5])
    async def _step_request(self,packet,sequence,epoch=None):
        async with self.operation_lock:
            if epoch is not None and epoch!=self.motor_stop_epoch:
                raise RuntimeError('Step cancelled by stop before transmission')
            reply=await self._request(packet,lambda r:r.destination==1 and r.subservice==0
                         and len(r.payload)==20 and r.payload[:3]==bytes((0x20,0x79,sequence)))
        status=decode_step_status(reply.payload)
        self.event('step_status',**status)
        if status['result']:
            raise RuntimeError(f'Step request refused: status {status["result"]}')
        return status
    def _step_gate(self):
        self._motor_gate()
        if self.version not in STEP_VERSIONS:
            raise RuntimeError('Speed-controller steps require installed .016 or later; no request sent')
    async def step_status(self):
        """Read-only: step ownership, stock motion flags and wheel encoder positions."""
        self._step_gate();seq=self._sequence()
        return await self._step_request(frame(1,0,bytes((0x20,0x79,seq))),seq)
    async def step(self,kind,a=0,b=0,up=0,down=0,ms=300,epoch=None):
        """Start one speed-controller step; returns once the robot accepted it."""
        self._step_gate();seq=self._sequence()
        packet=step_frame(seq,kind,a,b,up,down,ms) # validate before any effects
        await self._control_ready()
        self.motor_may_be_active=True
        self.event('step_requested',step_kind=kind,a=a,b=b,up=up,down=down,ms=ms,sequence=seq)
        return await self._step_request(packet,seq,epoch=self.motor_stop_epoch if epoch is None else epoch)
    async def moves(self,steps):
        """Run up to 32 items one after another, waiting for each move to finish.

        Items are described in plan_moves. Eyes and sound do not wait.
        The firmware cuts a step when host traffic stops; stop ends the list early.
        """
        self._step_gate()
        plan=plan_moves(steps)
        epoch=self.motor_stop_epoch;ran=0;last=None;stopped_early=False
        try:
            for args in plan:
                if epoch!=self.motor_stop_epoch:stopped_early=True;break
                if args[0]=='wait':
                    await asyncio.sleep(args[1]/1000);ran+=1;continue
                if args[0]=='eyes':
                    await self.eyes(args[1],args[2]);ran+=1;continue
                if args[0]=='sound':
                    await self.play_file(args[1]);ran+=1;continue
                if args[0]=='tune':
                    # Each note is due on the beat, so request latency does not stretch the tune.
                    start=time.monotonic()
                    for i,path in enumerate(args[1]):
                        if epoch!=self.motor_stop_epoch:stopped_early=True;break
                        if path:await self.play_file(path)
                        await asyncio.sleep(max(0,start+(i+1)*args[2]/1000-time.monotonic()))
                    if stopped_early:break
                    ran+=1;continue
                await self.step(*args,epoch=epoch)
                # From idle the stock controller ramps for 200 ms regardless of the step time.
                await asyncio.sleep(max(args[5],200)/1000)
                deadline=time.monotonic()+1
                while True:
                    if epoch!=self.motor_stop_epoch:stopped_early=True;break
                    last=await self.step_status()
                    if not last['owned']:break
                    if time.monotonic()>deadline:raise RuntimeError('Step did not finish in time')
                    await asyncio.sleep(.03)
                if stopped_early:break
                if last['reason']!='finished':
                    raise RuntimeError(f'Step ended early: {last["reason"]}')
                ran+=1
        except BaseException:
            if not self.failure and self.client and self.client.is_connected and epoch==self.motor_stop_epoch:
                await self.stop_motors()
            raise
        self.motor_may_be_active=False if not stopped_early else self.motor_may_be_active
        return dict(steps_run=ran,stopped_early=stopped_early,last=last)
    async def pulse(self,left=0,right=0,duration_ms=250):
        self._motor_gate();seq=self._sequence()
        epoch=self.motor_stop_epoch
        packet=pulse_frame(seq,left,right,duration_ms) # validate before any effects
        await self._control_ready()
        self.motor_may_be_active=True # even a lost ACK may accompany an accepted pulse
        self.event('motor_pulse_requested',left=left,right=right,duration_ms=duration_ms,sequence=seq)
        return await self._motor_request(packet,0x7c,seq,epoch=epoch)
    async def drive(self,left=0,right=0,total_ms=600):
        """Drive for total_ms by chaining <=250 ms pulses (needs .014+ renewal).

        left/right are the physical wheels (channel 0/1), positive forward. The
        firmware still ends motion 250 ms after the last accepted pulse, and a
        stop from another task ends the drive early. Always finishes with a stop.
        """
        self._motor_gate()
        if self.version not in DIAGNOSTIC_VERSIONS:
            raise RuntimeError('Chained driving requires installed .014 or later; no motor request sent')
        if type(total_ms) is not int or not 50<=total_ms<=3000:
            raise ValueError('Drive time must be 50..3000 ms')
        pulse_frame(0,left,right,250) # validate before any effects
        if not (left or right):raise ValueError('Use stop for zero power')
        epoch=self.motor_stop_epoch
        await self._control_ready()
        self.event('drive_requested',left=left,right=right,total_ms=total_ms)
        deadline=time.monotonic()+total_ms/1000;pulses=0;stopped_early=False
        try:
            while (remaining:=deadline-time.monotonic())>.005:
                began=time.monotonic();seq=self._sequence()
                ms=min(250,max(10,-(-int(remaining*1000)//10)*10))
                self.motor_may_be_active=True
                try:
                    await self._motor_request(pulse_frame(seq,left,right,ms),0x7c,seq,epoch=epoch)
                except RuntimeError:
                    if epoch!=self.motor_stop_epoch:stopped_early=True;break
                    raise
                pulses+=1
                # Renew every 150 ms so a 250 ms pulse never lapses between sends.
                await asyncio.sleep(max(0,min(.15,deadline-time.monotonic())-(time.monotonic()-began)))
                if epoch!=self.motor_stop_epoch:stopped_early=True;break
        finally:
            if not self.failure and self.client and self.client.is_connected and not stopped_early:
                await self.stop_motors()
        return dict(pulses=pulses,stopped_early=stopped_early,left=left,right=right,total_ms=total_ms)
    async def stop_motors(self):
        self.motor_stop_epoch+=1
        self._motor_gate();seq=self._sequence()
        # Stop never depends on fresh presence/lease; firmware accepts it unconditionally.
        result=await self._motor_request(pulse_frame(seq,0,0,0),0x7c,seq,priority=True)
        if result['active'] or result['left'] or result['right'] or result['mode']:
            raise RuntimeError('Stop did not report zero motor commands and idle mode')
        self.motor_may_be_active=False
        return result
    async def _close(self):
        try:
            if self.motor_may_be_active and self.client and self.client.is_connected:
                if self.failure:
                    self.event('motor_cleanup_device_deadline',reason='Request failure; rely on MCU pulse deadline')
                else:
                    task=asyncio.create_task(self.stop_motors())
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
        finally:
            await super()._close()
