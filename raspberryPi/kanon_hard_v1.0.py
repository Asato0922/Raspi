import os
# ▼ これを gpiozero のインポートより前に必ず追加します ▼
os.environ["GPIOZERO_PIN_FACTORY"] = "lgpio"

import asyncio
import websockets
import json
from gpiozero import Button, OutputDevice, RotaryEncoder
from mfrc522 import SimpleMFRC522
import threading
import time
import board
import neopixel

# ★Pygameを削除し、OSC通信用のライブラリを追加
from pythonosc import udp_client

# ==========================================
# 1. 初期設定とデータの定義
# ==========================================
# ★Pdの受信ポート(8000)へ接続するクライアントを作成
osc_client = udp_client.SimpleUDPClient("127.0.0.1", 8000)

# ★WAVファイル名の代わりに、4和音のMIDIノート番号（ド=60）を定義
CHORD_MIDI_MAP = {
    "C": {
        "C":  [60, 64, 67, 48], # C (ドミソド)
        "Dm": [62, 65, 69, 50], # Dm (レファラレ)
        "Em": [64, 67, 71, 52], # Em (ミソシミ)
        "F":  [65, 69, 72, 53], # F (ファラドファ)
        "G":  [67, 71, 74, 55], # G (ソシレソ)
        "Am": [69, 72, 76, 57]  # Am (ラドミラ)
    }
}

# セブンスコード用のMIDIノート番号（ルート, 3度, 5度, 7度）を追加
CHORD_MIDI_MAP_7TH = {
    "C": {
        "C":  [60, 64, 67, 71], # Cmaj7 (ドミソシ)
        "Dm": [62, 65, 69, 72], # Dm7 (レファラド)
        "Em": [64, 67, 71, 74], # Em7 (ミソシレ)
        "F":  [65, 69, 72, 76], # Fmaj7 (ファラドミ)
        "G":  [67, 71, 74, 77], # G7 (ソシレファ)
        "Am": [69, 72, 76, 79]  # Am7 (ラドミソ)
    }
}

CHORD_LED_MAP = {
    "C":  [154, 205, 50],  # C (ドミソ) - 黄緑
    "Dm": [255, 255, 0],   # Dm (レファラ) - 黄色
    "Em": [255, 165, 0],   # Em (ミソシ) - オレンジ
    "F":  [255, 0, 0],     # F (ファラド) - 赤
    "G":  [255, 105, 180], # G (ソシレ) - ピンク
    "Am": [100, 149, 237]  # Am (ラドミ) - 青
}

CARD_MAPPING = {
    "584189029063": "C",
    "584190470610": "Dm",
    "584190601951": "Em",
    "584192436787": "F",
    "584192371250": "G",
    "584188832251": "Am"
}

current_scale = "C"
slot_cards = [None, None, None, None]
current_cs_index = 0

current_bpm = 120.0
is_metronome_on = False
beats_per_measure = 4

# ▼▼▼ 変更部分: コード（カード）単位でセブンス状態を管理する辞書 ▼▼▼
chord_is_seventh = {}
# コードが「最後に置かれていたスロット番号」を記憶する辞書（移動検知用）
chord_last_slot = {}

# ==========================================
# 2. ハードウェア（GPIO）の設定
# ==========================================
chord_buttons = [
    Button(2, bounce_time=0.02), Button(3, bounce_time=0.02), 
    Button(4, bounce_time=0.02), Button(5, bounce_time=0.02)
]

LED_PIN = board.D19
LED_COUNT = 12
LED_BRIGHTNESS = 0.3

pixels = neopixel.NeoPixel(
    LED_PIN, 
    LED_COUNT, 
    brightness=LED_BRIGHTNESS, 
    auto_write=False
)
pixels.fill((0, 0, 0))
pixels.show()

ctrl_buttons = {
    "btn_up": Button(17, bounce_time=0.02),
    "btn_down": Button(27, bounce_time=0.02),
    "btn_left": Button(22, bounce_time=0.02),
    "btn_right": Button(23, bounce_time=0.02),
    "btn_A": Button(24, bounce_time=0.02),
    "btn_B": Button(26, bounce_time=0.02),
    "btn_C": Button(14, bounce_time=0.02),    
    "btn_RE_push": Button(13, bounce_time=0.02),
    "btn_1" : chord_buttons[0],
    "btn_2" : chord_buttons[1], 
    "btn_3" : chord_buttons[2],
    "btn_4" : chord_buttons[3]  
}

encoder = RotaryEncoder(15, 18, max_steps=0)

cs_pins = [
    OutputDevice(12, active_high=False, initial_value=False), 
    OutputDevice(16, active_high=False, initial_value=False),
    OutputDevice(20, active_high=False, initial_value=False),
    OutputDevice(21, active_high=False, initial_value=False)
]

reader = SimpleMFRC522()

class SpiWrapper:
    def __init__(self, original_spi):
        self._spi = original_spi
    
    def xfer2(self, data):
        cs_pins[current_cs_index].on()
        res = self._spi.xfer2(data)
        cs_pins[current_cs_index].off()
        return res
    
    def __getattr__(self, name):
        return getattr(self._spi, name)

reader.READER.spi = SpiWrapper(reader.READER.spi)

print("NFCモジュールを初期化中...")
for i in range(4):
    current_cs_index = i
    reader.READER.MFRC522_Init()
    time.sleep(0.01)
print("NFCの初期化完了！")

# ==========================================
# 3. 音声再生ロジック（OSC送信へ変更）とLED制御
# ==========================================
playing_notes = [None, None, None, None]

def update_slot_leds(slot_idx):
    card = slot_cards[slot_idx]
    base_idx = slot_idx * 3
    left_led = base_idx
    center_led = base_idx + 1
    right_led = base_idx + 2

    if not card:
        pixels[left_led] = (0, 0, 0)
        pixels[center_led] = (0, 0, 0)
        pixels[right_led] = (0, 0, 0)
    else:
        color = CHORD_LED_MAP.get(card, [255, 255, 255])
        pixels[center_led] = color
        
        if playing_notes[slot_idx] is not None:
            pixels[left_led] = color
            pixels[right_led] = color
        else:
            pixels[left_led] = (0, 0, 0)
            pixels[right_led] = (0, 0, 0)
            
    pixels.show()

def play_slot_chord(slot_idx):
    card = slot_cards[slot_idx]
    if not card:
        return
    
    # ▼▼▼ 変更部分：対象のコード（カード）がセブンス状態かを確認 ▼▼▼
    is_seventh = chord_is_seventh.get(card, False)
    if is_seventh:
        notes = CHORD_MIDI_MAP_7TH[current_scale].get(card)
    else:
        notes = CHORD_MIDI_MAP[current_scale].get(card)

    if notes:
        playing_notes[slot_idx] = notes
        for note in notes:
            osc_client.send_message("/play/note", [note, 100])
        update_slot_leds(slot_idx)

def stop_slot_chord(slot_idx):
    notes = playing_notes[slot_idx]
    if notes:
        for note in notes:
            osc_client.send_message("/play/note", [note, 0])
        playing_notes[slot_idx] = None
        update_slot_leds(slot_idx)

for i, btn in enumerate(chord_buttons):
    btn.when_pressed = lambda idx=i: play_slot_chord(idx)
    btn.when_released = lambda idx=i: stop_slot_chord(idx)

def toggle_metronome():
    global is_metronome_on
    is_metronome_on = not is_metronome_on
    print(f"メトロノーム: {'ON' if is_metronome_on else 'OFF'}")

ctrl_buttons["btn_RE_push"].when_pressed = toggle_metronome

# ==========================================
# 4. バックグラウンド処理 (NFC & メトロノーム)
# ==========================================
def nfc_monitor_loop():
    global slot_cards, current_cs_index
    while True:
        for i in range(4):
            current_cs_index = i
            reader.READER.AntennaOn()
            try:
                id = reader.read_id_no_block()
                if id is not None:
                    uid_str = str(id)
                    card_data = CARD_MAPPING.get(uid_str, uid_str)
                    
                    if slot_cards[i] != card_data:
                        # ▼▼▼ 変更部分：別のスロットに移動したか判定し、リセットする処理 ▼▼▼
                        last_slot = chord_last_slot.get(card_data)
                        if last_slot is not None and last_slot != i:
                            chord_is_seventh[card_data] = False
                            print(f"🔄 {card_data} がスロット {last_slot+1} から {i+1} へ移動したため、3和音にリセット")
                        
                        # 新しいスロット位置を記憶
                        chord_last_slot[card_data] = i
                        
                        slot_cards[i] = card_data
                        print(f"💳 スロット {i+1} にセット: {card_data}")
                        update_slot_leds(i)
                else:
                    if slot_cards[i] is not None:
                        print(f"❌ スロット {i+1} から取り外されました: {slot_cards[i]}")
                        slot_cards[i] = None
                        update_slot_leds(i)
            finally:
                reader.READER.AntennaOff()
            time.sleep(0.01)
        time.sleep(0.1)

def metronome_worker_loop():
    next_beat = time.perf_counter()
    beat_count = 1
    
    while True:
        print(current_bpm)
        if is_metronome_on and current_bpm > 0:
            now = time.perf_counter()
            if now >= next_beat:
                beat_type = 1 if beat_count == 1 else 2
                osc_client.send_message("/play/metronome", beat_type)
                
                beat_count += 1
                if beat_count > beats_per_measure:
                    beat_count = 1
                
                interval = 60.0 / current_bpm
                next_beat += interval
                if next_beat < now:
                    next_beat = now + interval
            
            sleep_time = next_beat - time.perf_counter()
            if sleep_time > 0.005:
                time.sleep(0.005)
            elif sleep_time > 0:
                time.sleep(0.001)
        else:
            time.sleep(0.1)
            next_beat = time.perf_counter()
            beat_count = 1

# ==========================================
# 5. Unity連携用 WebSocketサーバー
# ==========================================
async def send_status(websocket):
    while True:
        response_data = {
            "current_scale": current_scale,
            "slots": [],
            "controller": {}
        }
        for i in range(4):
            card = slot_cards[i]
            # Unity側に現在のセブンス状態も送信
            is_7th = chord_is_seventh.get(card, False) if card else False
            response_data["slots"].append({
                "slot_id": i, 
                "card": card, 
                "chord_file": card,
                "is_seventh": is_7th
            })
            
        for btn_name, btn_obj in ctrl_buttons.items():
            response_data["controller"][btn_name] = btn_obj.is_pressed
            
        response_data["controller"]["rotary_value"] = encoder.steps
            
        try:
            await websocket.send(json.dumps(response_data))
            await asyncio.sleep(0.016)
        except websockets.exceptions.ConnectionClosed:
            break

async def receive_commands(websocket):
    global current_scale, current_bpm, beats_per_measure
    try:
        async for message in websocket:
            data = json.loads(message)
            if data.get("action") == "set_scale":
                selected = data.get("scale")
                if selected in CHORD_MIDI_MAP:
                    current_scale = selected
                    print(f"スケール変更: {current_scale}")
            elif data.get("action") == "set_bpm":
                new_bpm = data.get("bpm")
                if new_bpm and new_bpm > 0:
                    current_bpm = float(new_bpm)
            elif data.get("action") == "set_volume":
                try:
                    new_volume = float(data.get("volume"))
                except (TypeError, ValueError):
                    continue
                if 0.0 <= new_volume <= 1.0:
                    osc_client.send_message("/play/volume", new_volume)
                    print(f"スロット音量: {new_volume:.2f}")
            elif data.get("action") == "set_beats":
                new_beats = data.get("beats")
                if new_beats and type(new_beats) == int and new_beats > 0:
                    beats_per_measure = new_beats
                    print(f"拍子変更: {beats_per_measure}拍子")
                    
            # ▼▼▼ 変更部分：スロットではなく「カード名」でセブンスを切り替える ▼▼▼
            elif data.get("action") == "set_seventh":
                card_name = data.get("card")  # 例: "C", "Dm"
                is_seventh_flag = data.get("is_seventh")
                
                if card_name is not None:
                    chord_is_seventh[card_name] = is_seventh_flag
                    print(f"コード {card_name} セブンス: {'ON' if is_seventh_flag else 'OFF'}")
                    
    except websockets.exceptions.ConnectionClosed:
        pass

async def handler(websocket):
    print("Unityが接続しました！")
    send_task = asyncio.create_task(send_status(websocket))
    recv_task = asyncio.create_task(receive_commands(websocket))
    
    done, pending = await asyncio.wait(
        [send_task, recv_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    print("Unityが切断されました")

async def main():
    print("アケコン WebSocketサーバー起動中 (ws://0.0.0.0:5000) ...")
    
    nfc_thread = threading.Thread(target=nfc_monitor_loop, daemon=True)
    nfc_thread.start()
    
    metro_thread = threading.Thread(target=metronome_worker_loop, daemon=True)
    metro_thread.start()
    
    async with websockets.serve(handler, "", 5000, ping_interval=None, ping_timeout=None):
        await asyncio.Future()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        pixels.fill((0, 0, 0))
        pixels.show()