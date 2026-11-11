"""Host-side diagnostics decoding and pulse handling against a simulated robot. No hardware."""
import unittest
from unittest.mock import patch
from mtiny_movement import (MTinyMovement,decode_diagnostics_page,diagnostics_page_frame)
from mtiny_protocol import frame
from test_mtiny_controller import scan
from test_mtiny_movement import MotorPeripheral

def units(value, count):
    return bytes((value >> (7 * i)) & 127 for i in range(count))


class DecodeTests(unittest.TestCase):
    def test_version_page(self):
        body = bytes((0x20, 0x7a, 3, 0, 0, 1, 28, 1, 1, 6)) + units(500, 5) + units(10500, 5)
        radio = decode_diagnostics_page(body)
        self.assertEqual((radio['component'], radio['version'], radio['replies'], radio['age_ms'],
                          radio['uptime_ms']), ('car_radio', '28.01.001.006', 1, 500, 10500))

    def test_version_page_without_reply_has_no_version(self):
        body = bytes((0x20, 0x7a, 3, 1, 0)) + bytes(15)
        self.assertIsNone(decode_diagnostics_page(body)['version'])

    def test_update_traffic_pages(self):
        counts = b''.join(units(n, 3) for n in (0, 1, 0, 851, 1, 0))
        sent = decode_diagnostics_page(bytes((0x20, 0x7a, 3, 4, 0)) + counts)
        self.assertEqual((sent['frames']['upgrade_request'], sent['frames']['file_info_or_data'],
                          sent['frames']['end']), (1, 851, 1))
        counts = b''.join(units(n, 3) for n in (0, 1, 0, 0, 1, 0))
        got = decode_diagnostics_page(bytes((0x20, 0x7a, 3, 5, 0)) + counts + bytes((8, 0, 0, 0)) + units(0, 5))
        self.assertEqual((got['frames']['upgrade_request'], got['frames']['end'], got['last_subservice'],
                          got['last_payload']), (1, 1, 8, [0, 0, 0]))

    def test_bad_requests(self):
        with self.assertRaises(ValueError):
            diagnostics_page_frame(1, 6)
        with self.assertRaises(ValueError):
            decode_diagnostics_page(b'\x20\x7a\x01\x06')


class Peripheral014(MotorPeripheral):
    async def write_gatt_char(self,char,data,response):
        if data[:6]==b'\xf0\x01\x71\0\x20\x7a':
            seq,page=data[6],data[7]
            self.send(frame(1,0,bytes((0x20,0x7a,seq,page,0))+bytes(15 if page<4 else 18 if page==4 else 27)))
        elif data[:6]==b'\xf0\x01\x71\0\x20\x03':self.asked=True
        else:await super().write_gatt_char(char,data,response)

class HostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches=[patch('mtiny_controller.BleakScanner.find_device_by_filter',scan),
                      patch('mtiny_controller.BleakClient',Peripheral014)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        MotorPeripheral.version=b'28.01.005.013.7100'
    async def test_report_requires_014(self):
        MotorPeripheral.version=b'28.01.005.013.7100'
        async with MTinyMovement() as robot:
            with self.assertRaises(RuntimeError):await robot.diagnostics_page(0)
    async def test_report_and_pulses_on_014(self):
        MotorPeripheral.version=b'28.01.005.014.7100'
        async with MTinyMovement() as robot:
            report=await robot.component_report(ask_radio=True)
            self.assertTrue(robot.client.asked)
            self.assertEqual(set(report['versions']),{'car_radio','pen_radio','pen_main','audio'})
            self.assertEqual((await robot.pulse(150,0))['left'],150)
            self.assertEqual((await robot.pulse(200,200))['right'],200)
            await robot.stop_motors()
    async def test_drive_chains_pulses_then_stops(self):
        MotorPeripheral.version=b'28.01.005.014.7100'
        async with MTinyMovement() as robot:
            result=await robot.drive(200,200,500)
            writes=robot.client.motor_writes
            self.assertGreaterEqual(result['pulses'],3)
            self.assertEqual(writes[-1][7:10],b'\x40\x40\0')
            self.assertTrue(all(w[7:9]==b'\x54\x54' for w in writes[-1-result['pulses']:-1]))
            with self.assertRaises(ValueError):await robot.drive(200,200,3001)
            with self.assertRaises(ValueError):await robot.drive(0,0,500)
        MotorPeripheral.version=b'28.01.005.013.7100'
        async with MTinyMovement() as robot:
            with self.assertRaises(RuntimeError):await robot.drive(200,200,500)

if __name__=='__main__':unittest.main()
