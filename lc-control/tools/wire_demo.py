"""一键协议演示：只启动本机虚构 CDU，通过真实 TCP 跑完整产品循环，保存动作与退出接管证据。
不连接任何真实设备；需要允许本机临时回环端口。"""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lc_control.configuration import load_scene
from lc_control.emulator import Emulator
from lc_control.operations import run

root=Path(__file__).resolve().parents[1]
scene=load_scene(root/'examples/modbus_emulator.json')
emulator=Emulator(scene,port=0).start()
scene['devices']['CDU_01']['connection']['port']=emulator.port
output=root/'outputs/wire-demo'
try:
    result=run(scene,output,'control',steps=4,interval=5)
    result['device_writes']=emulator.writes
    result['device_mode_after_shutdown']=emulator.plant.operating_mode
    result['evidence']='Real Modbus TCP exchange on loopback; synthetic device, no OEM hardware.'
    (output/'wire-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
finally:
    emulator.close()
