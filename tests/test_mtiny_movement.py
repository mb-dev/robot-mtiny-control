"""Simulated host failure/expiry tests. No hardware or BLE access."""
import asyncio
import unittest
import time
from unittest.mock import patch
from mtiny_movement import MTinyMovement,pulse_frame
from mtiny_protocol import frame
from test_mtiny_controller import Peripheral,scan

class MotorPeripheral(Peripheral):
    version=b'28.01.005.013.7100'
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw);self.power=[0,0];self.until=0;self.reason=0;self.motor_writes=[];self.drop_motor=False
    async def write_gatt_char(self,char,data,response):
        # Motor frames are at most12 bytes; other frame fragments use base decoder.
        if data[:6] in (b'\xf0\x01\x71\0\x20\x7b',b'\xf0\x01\x71\0\x20\x7c'):
            body=data[4:-2];selector,seq=body[1:3]
            if time.monotonic()>=self.until and self.power!=[0,0]:self.power=[0,0];self.reason=2
            if selector==0x7c:
                self.motor_writes.append(bytes(data))
                self.power=[(body[3]-64)*10,(body[4]-64)*10];self.until=time.monotonic()+body[5]/100
                self.reason=0 if body[5] else 1
                if self.drop_motor:return
            enc=lambda v:bytes(((v&65535)>>(i*7))&127 for i in range(3))
            self.send(frame(1,0,bytes((0x20,selector,seq,0,int(self.power!=[0,0]),self.reason))+
                            enc(self.power[0])+enc(self.power[1])+b'\0'))
        else:await super().write_gatt_char(char,data,response)

class MovementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches=[patch('mtiny_controller.BleakScanner.find_device_by_filter',scan),
                      patch('mtiny_controller.BleakClient',MotorPeripheral)]
        for p in self.patches:p.start()
        MotorPeripheral.version=b'28.01.005.013.7100'
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    async def test_stop_and_expiry(self):
        async with MTinyMovement() as robot:
            self.assertEqual((await robot.pulse(150,0))['left'],150)
            self.assertEqual((await robot.stop_motors())['reason'],1)
            await robot.pulse(0,-150)
            await robot.hold(.3)
            status=await robot.motor_status()
            self.assertEqual((status['active'],status['left'],status['right'],status['reason']),(0,0,0,2))
    async def test_lost_ack_never_replays_nonzero(self):
        async with MTinyMovement() as robot:
            robot.client.drop_motor=True
            with self.assertRaises(TimeoutError):await robot.pulse(150,0)
        self.assertEqual(len(robot.client.motor_writes),1)
        self.assertFalse(robot.client.is_connected)
    async def test_cancellation_stops_before_disconnect(self):
        robot=MTinyMovement()
        async def session():
            async with robot:
                await robot.pulse(150,0)
                await robot.hold(5)
        task=asyncio.create_task(session())
        while not getattr(robot.client,'motor_writes',[]):await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(robot.client.motor_writes[-1][7:10],b'\x40\x40\0')
        self.assertFalse(robot.client.is_connected)
    async def test_old_version_refuses_motor_request(self):
        MotorPeripheral.version=b'28.01.005.012.7100'
        async with MTinyMovement() as robot:
            with self.assertRaises(RuntimeError):await robot.pulse(150,0)
        self.assertFalse(robot.client.motor_writes)
    async def test_stop_bypasses_pending_motor_reply(self):
        async with MTinyMovement() as robot:
            robot.client.drop_motor=True
            pending=asyncio.create_task(robot.pulse(150,0))
            while not robot.client.motor_writes:await asyncio.sleep(.01)
            robot.client.drop_motor=False
            result=await asyncio.wait_for(robot.stop_motors(),.5)
            self.assertEqual(result['active'],0)
            self.assertFalse(pending.done())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):await pending
    async def test_stop_invalidates_pulse_waiting_for_preflight(self):
        async with MTinyMovement() as robot:
            began=asyncio.Event();release=asyncio.Event()
            async def delayed_ready():
                began.set();await release.wait()
            with patch.object(robot,'_control_ready',delayed_ready):
                pending=asyncio.create_task(robot.pulse(150,0))
                await began.wait()
                await robot.stop_motors()
                release.set()
                with self.assertRaisesRegex(RuntimeError,'cancelled by stop'):await pending
            self.assertEqual(len(robot.client.motor_writes),1)
            self.assertEqual(robot.client.motor_writes[0][7:10],b'\x40\x40\0')
    def test_bounds(self):
        for left,right,ms in ((201,0,250),(0,-210,250),(150,0,0),(150,0,260),(150,0,251),(15,0,100)):
            with self.assertRaises(ValueError):pulse_frame(1,left,right,ms)

if __name__=='__main__':unittest.main()
