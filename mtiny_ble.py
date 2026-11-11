"""Low-level mTiny eye-animation helper (no activity lease; see mtiny_controller for the full client).
Transport acceptance is distinct from physical execution. No motors, firmware,
pairing or power commands. Application route: host destination 0x7d (audio/display MCU).
"""
from bleak import BleakScanner,BleakClient
NUS='6e400001-b5a3-f393-e0a9-e50e24dcca9e'
RX='6e400002-b5a3-f393-e0a9-e50e24dcca9e'
TX='6e400003-b5a3-f393-e0a9-e50e24dcca9e'

def eye_frame(animation_a=4,animation_b=4):
    if any(type(n) is not int or not 0<=n<=127 for n in (animation_a,animation_b)):
        raise ValueError('Animation IDs are integers 0..127; the robot ignores IDs it has no animation for')
    body=bytes([0x7d,0x71,1,0x30,animation_a&127,animation_a>>7,animation_b&127,animation_b>>7])
    return b'\xf0'+body+bytes([sum(body)&127,0xf7])

class MTiny:
    def __init__(self,event=None):
        self.event=event or (lambda kind,**fields:None);self.client=None
    async def __aenter__(self):
        device=await BleakScanner.find_device_by_filter(
            lambda d,a:(a.local_name or d.name)=='m_tiny_car' and NUS in a.service_uuids,timeout=12)
        if device is None:raise RuntimeError('mTiny not found')
        self.client=BleakClient(device,timeout=10)
        try:
            await self.client.connect()
            self.event('connected',identifier=device.address)
            await self.client.start_notify(TX,lambda c,b:self.event('notification',hex=bytes(b).hex()))
        except BaseException:
            if self.client.is_connected:await self.client.disconnect()
            raise
        return self
    async def eyes(self,animation_a=4,animation_b=4):
        frame=eye_frame(animation_a,animation_b)
        if self.client is None or not self.client.is_connected:raise RuntimeError('Not connected')
        self.event('eye_write_started',animation_a=animation_a,animation_b=animation_b,frame_hex=frame.hex())
        await self.client.write_gatt_char(RX,frame,response=True)
        self.event('eye_write_gatt_acknowledged',physical_effect_confirmed=False)
    async def __aexit__(self,*exc):
        if self.client and self.client.is_connected:await self.client.disconnect()
        self.event('disconnected_intentionally')
