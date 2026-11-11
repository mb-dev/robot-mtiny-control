"""Host encoding/decoding of .016 speed-controller steps. No hardware, no firmware."""
import unittest
from mtiny_movement import step_frame,decode_step_status,plan_moves,STEP_KINDS

class StepFirmwareTests(unittest.TestCase):
    def test_every_kind_encodes(self):
        for kind, number in STEP_KINDS.items():
            packet = step_frame(1, kind, 100, 0, 0, 0, 300)
            self.assertEqual(packet[4:6], b'\x20\x79')
            self.assertEqual((packet[7], packet[8], packet[9]), (number, 64 + 20, 64))

    def test_status_decodes_negative_encoder(self):
        def units(value, count):
            return bytes((value >> (7 * i)) & 127 for i in range(count))
        body = bytes((0x20, 0x79, 5, 0, 1, 0, 2, 0, 0, 5)) + units(0xfffffffe, 5) + units(373, 5)
        status = decode_step_status(body)
        self.assertEqual((status['sequence'], status['owned'], status['reason'], status['mode']),
                         (5, 1, 'running', 2))
        self.assertEqual((status['left_encoder'], status['right_encoder']), (-2, 373))

    def test_validation(self):
        for args in ((1,'spin'),(1,'forward',201),(1,'forward',102),(1,'forward',100,0,7),(1,'forward',100,0,0,0,19),
                     (1,'forward',100,0,0,0,5001),(1,'forward',100,0,200,200,300),(128,'forward')):
            with self.assertRaises(ValueError):step_frame(*args)
        with self.assertRaises(ValueError):decode_step_status(bytes(13))
    def test_plan_moves_distances_angles_and_extras(self):
        plan=plan_moves([dict(kind='forward',mm=203),dict(kind='left',deg=90,speed=90),dict(kind='left',deg=360),
                         dict(kind='right',deg=25),dict(kind='eyes',a=4),dict(kind='sound',path='system/a.mp3'),
                         dict(kind='wait',ms=100),dict(kind='backward',a=50,ms=300)])
        # The first three reproduce the floor measurements of 2026-11-11 (2000 ms was sent for 203 mm).
        self.assertEqual(plan[0],('forward',100,0,0,40,2017))
        self.assertEqual(plan[1:3],[('left',90,0,0,40,1120),('left',180,0,0,40,2120)])
        self.assertEqual(plan[3],('right',135,0,0,40,305)) # slowed so the step is not shorter than 300 ms
        self.assertEqual(plan[4:],[('eyes',4,4),('sound','system/a.mp3'),('wait',100),('backward',50,0,0,0,300)])
        tune=plan_moves([dict(kind='tune',notes='C4 - F5',beat=300)])
        self.assertEqual(tune,[('tune',('system/subject/music/0x850.mp3',None,'system/subject/music/0x85A.mp3'),300)])
        with self.assertRaises(ValueError):plan_moves([dict(kind='tune',notes='C9')])
        for bad in ([],[dict(kind='forward',deg=90)],[dict(kind='left',mm=10)],[dict(kind='forward',mm=2000,speed=20)],
                    [dict(kind='forward',mm=10,ms=300)],[dict(kind='sound',path='../x')],[dict(kind='eyes',a=200)],
                    [dict(kind='wait',ms=1)]*33,[dict(kind='forward',mm=1.5)]):
            with self.assertRaises(ValueError):plan_moves(bad)

if __name__=='__main__':unittest.main()
