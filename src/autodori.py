import argparse
import datetime
import json
import logging
import random
import re
import string
import subprocess
import sys
import threading
import time
import queue
from pathlib import Path
from typing import Optional, Union
from PIL import Image

import requests
import yaml
from fuzzywuzzy import fuzz, process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction, CustomRecognitionResult
from maa.custom_recognition import CustomRecognition
from maa.define import RectType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import AdbDevice, Toolkit
from minitouchpy import (MNT, MNTEvATive7LogEventData, MNTEvent, MNTEventData,
                        MNTServerCommunicateType)

# --- 路径修复：确保在打包后也能找到文件 ---
def get_base_path():
    """获取脚本或打包后程序的基础路径。"""
    if getattr(sys, 'frozen', False):
        # 如果是打包后的 .exe 文件
        return Path(sys.executable).parent
    else:
        # 如果是普通的 .py 脚本
        return Path(__file__).parent

BASE_PATH = get_base_path()

import player
from api import BestdoriAPI
# 从 util.py 导入, 而不是在本地定义
from util import *

# --- 全局配置和状态管理 ---
class AppState:
    """一个线程安全的类，用于保存共享的应用程序状态。"""
    def __init__(self):
        self.config = {
            "server_name": None,
            "difficulty": "hard",
            "livemode": "freelive",
            "min_liveboost": 1,
            "debug": False,
        }
        self.automation_running = threading.Event()
        self.tasker_instance: Optional[Tasker] = None
        self.log_queue = queue.Queue()
        self.debug_queue = queue.Queue()

app_state = AppState()

# --- 路径和数据库初始化 ---
data_path = BASE_PATH / "data"
data_path.mkdir(exist_ok=True)
cache_path = BASE_PATH / "cache"
cache_path.mkdir(exist_ok=True)
config_path = data_path / "config.yml"
(BASE_PATH / "debug").mkdir(exist_ok=True)
if not config_path.exists():
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")

from peewee import *
from playhouse.sqlite_ext import JSONField

db = SqliteDatabase(data_path / "play_records.db")

class PlayRecord(Model):
    class Meta:
        database = db
    play_time = TimestampField()
    play_offset = JSONField()
    chart_id = CharField()
    difficulty = CharField()
    succeed = BooleanField()
    result = JSONField()

db.connect()
db.create_tables([PlayRecord], safe=True)

from chart import Chart

# --- 全局变量 ---
MIN_LIVEBOOST = 1
LIVEMODE = "freelive"
DIFFICULTY = "hard"
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
MAX_FAILED_TIMES = 10
CMD_SLICE_SIZE = 100

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()
maacontroller: AdbController = None
device: AdbDevice = None
current_player: player.Player = None
current_orientation: int = 0
mnt: MNT = None
all_songs: dict = BestdoriAPI.get_song_list()
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: str = None
current_song_id: str = None
current_chart: Chart = None
play_failed_times: int = 0
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list[MNTEvATive7LogEventData] = []
cmd_log_list_lock = threading.Lock()
current_version = None
game_package_name: str = None
chosen_resource_name: Optional[str] = None

# ... (从此处到 main 函数之前的所有函数定义，如 QueueHandler, reset_callback_data, MAA自定义任务等，都与上一版本相同，为简洁起见省略)
# --- Custom Logging Handler for GUI ---

class QueueHandler(logging.Handler):
    """A custom logging handler that puts logs into a queue."""
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


def reset_callback_data():
    global callback_data
    callback_data = {
        "wait": {"total": 0, "total_offset": 0.0},
        "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "interval": {"total": 0, "total_offset": 0.0},
        "last_cmd_endtime": -1,
    }

reset_callback_data()

def check_song_available(name, id_, difficulty):
    if name.startswith("[FULL]"):
        return False
    lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
    return not lastmatched or not lastmatched.succeed

@maaresource.custom_recognition("SongRecognition")
class SongRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [198, 333, 372, 29]
        
        def match(model=None):
            pplname = "_ocrsong_" + "".join(random.choices(string.ascii_lowercase, k=7))
            pipeline = {
                pplname: {
                    "recognition": "OCR",
                    "only_rec": True,
                    "roi": roi,
                },
            }
            if model != None:
                pipeline[pplname]["model"] = model
            try:
                rec_result = context.run_recognition(
                    pplname,
                    argv.image,
                    pipeline,
                )
                song_fuzzyname = rec_result.best_result.text
                similarity = rec_result.best_result.similarity
            except:
                song_fuzzyname = ""
                similarity = 0
            
            if app_state.config['debug']:
                img_pil = Image.fromarray(argv.image)
                roi_img = img_pil.crop((roi[0], roi[1], roi[0]+roi[2], roi[1]+roi[3]))
                debug_data = {
                    "type": "recognition", "name": "SongRecognition",
                    "image": roi_img, "text": song_fuzzyname,
                    "similarity": similarity
                }
                app_state.debug_queue.put(debug_data)
            
            return fuzzy_match_song(song_fuzzyname)

        jpmatch = match("ppocr_v5/zh_cn")
        commonmatch = match()
        
        logging.debug(f"PPOCRv5匹配结果: {jpmatch}, 默认匹配结果: {commonmatch}")
        result = sorted([jpmatch, commonmatch], key=lambda x: x[1], reverse=True)
        
        if all([r[1] < 50 for r in result]):
            return CustomRecognition.AnalyzeResult(None, "")
        result_music_name = result[0][0]

        if not check_song_available(result_music_name, all_song_name_indexes[result_music_name], DIFFICULTY):
            return CustomRecognition.AnalyzeResult(None, "")

        return CustomRecognition.AnalyzeResult(roi, result_music_name)

@maaresource.custom_recognition("LiveBoostEnoughRecognition")
class LiveBoostEnoughRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        roi = [979, 30, 61, 20]
        pipeline = {"live_boost_enough_ocr": {"recognition": "OCR", "only_rec": True, "roi": roi}}
        live_boost_text = context.run_recognition("live_boost_enough_ocr", argv.image, pipeline).best_result.text
        logging.debug(f"体力值识别结果: {live_boost_text}")
        match = re.match(r"^\s*(\d+)\s*/", live_boost_text.replace(" ", ""))
        live_boost = int(match.group(1)) if match else -1
        logging.debug(f"解析后体力值: {live_boost}")
        return CustomRecognition.AnalyzeResult(roi, str(live_boost))

@maaresource.custom_action("HandleLiveBoost")
class HandleLiveBoost(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        liveboost = int(argv.reco_detail.best_result.detail)
        if liveboost < MIN_LIVEBOOST:
            logging.info("体力不足，准备退出")
            context.run_action("close_app")
            context.run_action("stop")
        return CustomAction.RunResult(True)

@maaresource.custom_recognition("PlayResultRecognition")
class PlayResultRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        types = {"score": [1028, 192, 144, 35], "maxcombo": [1009, 391, 91, 28], "perfect": [829, 282, 90, 28], "great": [828, 322, 91, 27], "good": [829, 363, 91, 27], "bad": [829, 401, 90, 27], "miss": [830, 438, 91, 28], "fast": [1088, 283, 90, 27], "slow": [1088, 323, 91, 28]}
        result = {}
        pipeline = {f"_PlayResultRecognition_ocr_{k}": {"recognition": "OCR", "only_rec": True, "roi": v} for k, v in types.items()}
        
        for type_name, roi in types.items():
            try:
                rec_result = context.run_recognition(f"_PlayResultRecognition_ocr_{type_name}", argv.image, pipeline)
                ocrtext = rec_result.best_result.text
                result[type_name] = int(ocrtext)
                if app_state.config['debug']:
                    img_pil = Image.fromarray(argv.image)
                    roi_img = img_pil.crop((roi[0], roi[1], roi[0]+roi[2], roi[1]+roi[3]))
                    app_state.debug_queue.put({"type": "recognition", "name": f"PlayResult_{type_name}", "image": roi_img, "text": ocrtext, "similarity": rec_result.best_result.similarity})
            except:
                result[type_name] = -1
        
        logging.debug(f"演奏结果: {result}")
        return CustomRecognition.AnalyzeResult([0, 0, 0, 0], json.dumps(result))

@maaresource.custom_action("SavePlayResult")
class SavePlayResult(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            global current_song_id, play_failed_times
            succeed = json.loads(argv.custom_action_param).get("succeed")
            playresult = json.loads(argv.reco_detail.best_result.detail) if succeed else {}
            if not succeed: play_failed_times += 1
            PlayRecord.create(play_time=int(time.time()), play_offset=OFFSET, result=playresult, succeed=succeed, chart_id=current_song_id, difficulty=DIFFICULTY)
            if play_failed_times >= MAX_FAILED_TIMES:
                logging.error("失败次数超过最大限制")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"保存演奏结果失败: {e}")
            return CustomAction.RunResult(False)

@maaresource.custom_action("Play")
class Play(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            play_song()
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"演奏歌曲时失败: {e}", stack_info=True)
            return CustomAction.RunResult(False)

@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        name = argv.reco_detail.best_result.detail
        save_song(name)
        return CustomAction.RunResult(True)

# --- Automation Runner Class ---
class AutomationRunner(threading.Thread):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.daemon = True

    def run(self):
        global DIFFICULTY, MIN_LIVEBOOST, LIVEMODE, mnt
        DIFFICULTY, LIVEMODE, MIN_LIVEBOOST = self.state.config['difficulty'], self.state.config['livemode'], self.state.config['min_liveboost']
        self.state.automation_running.set()
        logging.info(f"自动化线程已启动，配置: {self.state.config}")
        try:
            init_maa()
            if not self.state.automation_running.is_set(): return
            self.state.tasker_instance = maatasker
            init_player_and_mnt()
            if not self.state.automation_running.is_set(): return
            logging.info("正在启动 MAA 任务...")
            task_result = maatasker.post_task("main", _get_override_pipeline()).wait()
            if task_result and not task_result.succeeded:
                logging.error(f"MAA 任务失败: {task_result.message}")
        except Exception as e:
            logging.error(f"自动化线程崩溃: {e}", exc_info=True)
        finally:
            if 'mnt' in globals() and mnt: mnt.stop()
            self.state.automation_running.clear()
            logging.info("自动化线程已结束。")

    def stop_automation(self):
        logging.info("收到停止信号。正在停止 MAA 任务...")
        self.state.automation_running.clear()
        if self.state.tasker_instance: self.state.tasker_instance.stop()

def _get_orientation():
    try:
        cmd = [str(device.adb_path.absolute()), "-s", device.address, "shell", "dumpsys input|grep SurfaceOrientation"]
        output = subprocess.check_output(cmd, text=True)
        return int(re.search(r"SurfaceOrientation:\s*(\d+)", output).group(1))
    except Exception as e:
        logging.error(f"获取 SurfaceOrientation 失败: {e}")
        return 0

def save_song(name):
    global current_song_name, current_song_id, current_chart, current_orientation
    current_song_name = name
    current_song_id = all_song_name_indexes[current_song_name]
    current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd((mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE)
    logging.debug(f"保存歌曲: {name}")

def play_song():
    logging.info("开始演奏")
    cmd_log_list.clear()
    reset_callback_data()

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE : index]:
            if action["type"] == "wait":
                wait_for += action["length"]
        return wait_for

    def _adjust_offset():
        global callback_data
        total_cost = 0.0
        for type_ in ["up", "down", "move", "wait", "interval"]:
            type_data = callback_data[type_]
            total = type_data["total"]
            if total != 0:
                total_cost += type_data["total_offset"] - OFFSET[type_] * total
                OFFSET[type_] = type_data["total_offset"] / total
        current_chart._a2c_offset += total_cost
        logging.debug(f"调整偏移: {OFFSET}")
        logging.debug(f"调整 _actions_to_cmd_offset: {total_cost}")

    wait_first_note()
    while app_state.automation_running.is_set():
        current_chart.command_builder.publish(mnt, block=False)
        time.sleep(max(0, _get_wait_time() - 3) / 1000)
        index = current_chart.actions_to_cmd_index
        if current_chart.actions[index : index + CMD_SLICE_SIZE]:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd((mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE)
        else:
            break
    time.sleep(2)


def wait_first_note():
    last_color = None
    waited_frames = 0
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False
    while app_state.automation_running.is_set():
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(cur_color[0:3] - last_color[0:3])
                logging.debug(f"图像变化: {change_score}")
                if change_score > 3:
                    if freezed:
                        logging.debug(f"第一个音符落在 {from_row}-{to_row} 之间")
                        time.sleep(PHOTOGATE_LATENCY / 1000)
                        break
                else:
                    if not freezed: waited_frames += 1
                if not freezed and waited_frames >= 200:
                    freezed = True
                    logging.debug("画面已冻结，等待第一个音符...")
            last_color = cur_color
        except Exception as e:
            logging.error(f"获取屏幕失败: {e}")


def init_maa():
    global game_package_name, chosen_resource_name, device, maacontroller
    try:
        interface_file = BASE_PATH / "assets/interface.json"
        if not interface_file.exists(): sys.exit("错误: assets/interface.json 未找到。")
        interface_data = json.loads(interface_file.read_text(encoding="utf-8"))
        resources = interface_data.get("resource", [])
        if not resources: sys.exit("错误: interface.json 中未定义任何资源。")
    except Exception as e:
        sys.exit(f"读取或解析 interface.json 失败: {e}")

    chosen_resource = None
    selected_server_name = app_state.config.get('server_name')

    if selected_server_name:
        chosen_resource = next((res for res in resources if res.get("name") == selected_server_name), None)
        if chosen_resource:
            logging.info(f"已通过配置选择服务器: {selected_server_name}")
        else:
            sys.exit(f"配置中指定的服务器 '{selected_server_name}' 在 interface.json 中未找到。")
    elif len(resources) == 1:
        chosen_resource = resources[0]
        logging.info(f"已自动选择唯一的可用资源: {chosen_resource.get('name', 'Unnamed')}")
    else:
        print("请选择要使用的资源:")
        for i, res in enumerate(resources): print(f"{i}: {res.get('name', 'Unnamed')}")
        try:
            selected_index = int(input("请输入选项对应的数字: "))
            chosen_resource = resources[selected_index]
        except (ValueError, IndexError):
            sys.exit("无效输入，请输入列表中的数字。正在退出。")
    
    chosen_resource_name = chosen_resource.get("name")
    app_state.config['server_name'] = chosen_resource_name
    package_map = {"b服": "com.bilibili.star.bili", "日服": "jp.co.craftegg.band"}
    game_package_name = package_map.get(chosen_resource_name, "jp.co.craftegg.band")
    if not package_map.get(chosen_resource_name):
        logging.warning(f"无法为资源 '{chosen_resource_name}' 确定包名，将默认使用 '{game_package_name}'。")
    
    logging.info(f"已选择资源: '{chosen_resource_name}'。游戏包名: '{game_package_name}'")

    for res_path_str in chosen_resource.get("path", []):
        resolved_path = res_path_str.replace("{PROJECT_DIR}", str(BASE_PATH))
        logging.info(f"正在从以下位置加载资源: {resolved_path}")
        if not maaresource.post_bundle(resolved_path).wait().succeeded:
            sys.exit(f"加载资源失败: {resolved_path}")
    
    Toolkit.init_option(str(BASE_PATH))
    adb_devices = Toolkit.find_adb_devices()
    if not adb_devices: sys.exit("未找到ADB设备。")
    
    _device: list[AdbDevice] = []
    for device_item in adb_devices:
        extra_names = device_item.config.get("extras", {}).keys()
        if "mumu" in extra_names or "ld" in extra_names:
            if (device_item.name, device_item.address) not in [(d.name, d.address) for d in _device]:
                _device.append(device_item)
    filter_str = config.get("device", {}).get("filter", "devices")
    _device = eval(filter_str, {}, {"devices": _device})
    if not _device: sys.exit("未找到支持的设备。")
    elif len(_device) == 1: device = _device[0]
    else:
        print("找到多个设备:")
        for i, dev in enumerate(_device): print(f"{i}: {dev.name}({dev.address})")
        device = _device[int(input("请选择一个设备: "))]
    
    maacontroller = AdbController(adb_path=device.adb_path, address=device.address, screencap_methods=device.screencap_methods, input_methods=device.input_methods, config=device.config)
    if not maacontroller.post_connection().wait().succeeded: sys.exit("连接控制器失败。")
    maatasker.bind(maaresource, maacontroller)
    if not maatasker.inited: sys.exit("MAA初始化失败。")
    logging.info("MAA初始化完成。")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data
        cmd, cost = data.cmd, data.cost
        with cmd_log_list_lock: cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]
        with callback_data_lock:
            if (last_end := callback_data.get("last_cmd_endtime")) != -1:
                callback_data["interval"]["total"] += 1
                callback_data["interval"]["total_offset"] += data.start_time - last_end
            callback_data["last_cmd_endtime"] = data.end_time
            if cmd_type == "w":
                callback_data["wait"]["total"] += 1
                callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
            elif cmd_type in "udm":
                type_ = {"u": "up", "d": "down", "m": "move"}[cmd_type]
                callback_data[type_]["uncommited"] += 1
                callback_data[type_]["total"] += 1
                callback_data[type_]["total_offset"] += cost
            elif cmd_type == "c":
                total_uncommitted = sum(callback_data[t]["uncommited"] for t in ["up", "down", "move"])
                if total_uncommitted != 0:
                    for t in ["up", "down", "move"]:
                        callback_data[t]["total_offset"] += cost * (callback_data[t]["uncommited"] / total_uncommitted)
                        callback_data[t]["uncommited"] = 0


def init_player_and_mnt():
    global current_player, mnt, device, game_package_name
    extra_config = device.config["extras"]
    type_ = "mumu" if "mumu" in extra_config else "ld"
    extra_config = extra_config[type_]
    current_player = player.Player(type_, Path(extra_config["path"]), extra_config["index"], game_package_name)
    mnt = MNT(device.address, type_="EvATive7", communicate_type=MNTServerCommunicateType.STDIO, mnt_asset_path=BASE_PATH / "assets/minitouch_EvATive7", callback=mnt_callback, adb_executor=str(device.adb_path.absolute()))
    logging.info("Mumu 和 MNT 初始化完成。")

def configure_log(log_queue: Optional[queue.Queue] = None):
    level = logging.INFO if log_queue else logging.DEBUG
    handlers = [logging.FileHandler(BASE_PATH / f"debug/autodori-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.log", "w", "utf-8")]
    handlers.append(QueueHandler(log_queue) if log_queue else logging.StreamHandler())
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S", handlers=handlers)

def _get_override_pipeline():
    global chosen_resource_name
    difficulty = DIFFICULTY
    roi = {"easy": [659, 495, 107, 97], "normal": [768, 494, 107, 97], "hard": [886, 494, 105, 97], "expert": [996, 493, 107, 97], "special": [1086, 449, 192, 184]}[difficulty]
    set_difficulty_pipeline = {"action": "Click", "recognition": "TemplateMatch", "template": [f"live/difficulty/{difficulty}_active.png", f"live/difficulty/{difficulty}_inactive.png"], "next": "get_song_name", "target": roi, "timeout": 5000, "interrupt": ["random_choice_song"]}
    
    livemode_map = {"日服": {"freelive": "フリーライブ", "challengelive": "チャレンジライブ"}, "b服": {"freelive": "自由演出", "challengelive": "挑战演出"}}
    server_livemode = livemode_map.get(chosen_resource_name, livemode_map["日服"])
    
    select_live_mode_pipeline = {
        "recognition": "OCR",
        "model": "ppocr_v5/zh_cn", # 统一使用v5多语言模型
        "expected": server_livemode.get(LIVEMODE), 
        "roi": [679, 183, 257, 354], 
        "action": "Click", 
        "post_delay": 1000, 
        "next": ["select_song", "select_live_mode", "live_home_button"], 
        "interrupt": ["login_expired", "connect_failed"]
    }
    
    return {"set_difficulty": set_difficulty_pipeline, "select_live_mode": select_live_mode_pipeline}

def get_current_version():
    global current_version
    try:
        current_version = json.loads((BASE_PATH / "assets/build_metadata.json").read_text("utf-8"))["version"]
    except:
        logging.debug("获取当前版本失败")

def check_update():
    logging.debug("正在检查更新...")
    try:
        latest_version = requests.get("https://api.github.com/repos/EvATive7/autodori/releases/latest").json()["tag_name"]
        logging.debug(f"当前版本: {current_version}, 最新版本: {latest_version}")
        if compare_semver(latest_version, current_version) == 1:
            print("\033[38;5;208m\033[1m有更新可用：{latest_version}，请前往 https://github.com/EvATive7/autodori/releases 下载。\033[0m")
            time.sleep(5)
    except Exception as e:
        logging.error(f"检查更新失败: {e}")

# --- 主入口 ---

# ==== UI Control (manual delay) support & FULL song unlock ====

# Override: allow FULL songs (remove previous restriction)
def check_song_available(name, id_, difficulty):
    """
    解除 FULL 类型限制：任何歌曲均可返回 True。
    （保留最近一次记录回避逻辑，若你仍需跳过已成功打过的曲，可在此处自行扩展。）
    """
    try:
        lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
        return (not lastmatched) or (not lastmatched.succeed)
    except Exception:
        # 数据库未就绪等情况，一律允许
        return True


class _ControlFileWatcher(threading.Thread):
    """
    读取 GUI 写入的控制文件，动态刷新以下全局量：
      - PHOTOGATE_LATENCY (ms)  —— 首音延迟
      - OFFSET['wait'] (s)     —— 手动补偿延迟（全局平移）
    并实现“变化限速”，避免一次性抖动过大。
    """
    def __init__(self, path: Union[str, Path], poll_interval: float = 0.2, max_step_ms: float = 20.0):
        super().__init__(daemon=True)
        self.path = Path(path) if path else Path("data/ui_control.json")
        self.poll = poll_interval
        self.max_step = max_step_ms
        self._stop = threading.Event()
        self.last_manual_s = 0.0

    def stop(self):
        self._stop.set()

    def run(self):
        global PHOTOGATE_LATENCY, OFFSET
        # 初始化：若文件不存在则创建默认
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(json.dumps({
                    "ready": False,
                    "first_note_delay_ms": PHOTOGATE_LATENCY,
                    "manual_comp_ms": int(OFFSET.get("wait", 0.0) * 1000),
                    "recalibrate_now": False
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        while not self._stop.is_set():
            try:
                ctrl = json.loads(self.path.read_text(encoding="utf-8"))
                # 1) 首音延迟：直接赋值
                if "first_note_delay_ms" in ctrl:
                    try:
                        PHOTOGATE_LATENCY = int(ctrl["first_note_delay_ms"])
                    except Exception:
                        pass

                # 2) 手动补偿延迟：做“限速”收敛（单位：秒）
                if "manual_comp_ms" in ctrl:
                    try:
                        target_s = float(ctrl["manual_comp_ms"]) / 1000.0
                        cur_s = float(OFFSET.get("wait", 0.0))
                        # 每个轮询周期最多改 max_step_ms
                        max_step_s = self.max_step / 1000.0
                        delta = max(-max_step_s, min(max_step_s, target_s - cur_s))
                        OFFSET["wait"] = cur_s + delta
                        self.last_manual_s = OFFSET["wait"]
                    except Exception:
                        pass
            except Exception:
                # 控制文件读取失败时忽略，继续轮询
                pass
            time.sleep(self.poll)


def _wait_until_ready(control_file: Optional[str], prompt: str = "等待 GUI 准备信号..."):
    """
    若设置了 --hold-for-ready，则在进入首音识别前等待 GUI 在 control-file 中写入 {"ready": true}
    """
    if not control_file:
        return
    path = Path(control_file)
    logging.info(prompt)
    while True:
        try:
            ctrl = json.loads(path.read_text(encoding="utf-8"))
            if ctrl.get("ready", False):
                # 复位 ready 标志，避免下一次误触发
                ctrl["ready"] = False
                path.write_text(json.dumps(ctrl, ensure_ascii=False, indent=2), encoding="utf-8")
                break
        except Exception:
            pass
        time.sleep(0.2)


def _capture_screen_to_file(outfile: Union[str, Path]):
    """
    初始化最小环境，然后截取一次屏幕保存到 outfile。
    """
    configure_log()
    init_maa()
    init_player_and_mnt()
    img = current_player.ipc_capture_display()
    try:
        Image.fromarray(img).save(str(outfile))
    except Exception:
        # 兼容（h,w,3) numpy 数组或已是 PIL Image
        if isinstance(img, Image.Image):
            img.save(str(outfile))
        else:
            raise
    logging.info(f"截屏已保存：{outfile}")


def _run_direct_mode(song_query: str, hold_for_ready: bool, control_file: Optional[str]):
    """
    直打模式：跳过 MAA 选曲界面，直接保存谱面并进 wait_first_note -> play_song。
    - song_query：歌名（可模糊）
    - hold_for_ready：进入 wait_first_note 前是否等待 GUI 准备
    - control_file：GUI 写入的控制文件路径
    """
    # 启动控制文件监听（若配置）
    watcher = _ControlFileWatcher(control_file) if control_file else None
    if watcher:
        watcher.start()

    try:
        configure_log()
        init_maa()
        init_player_and_mnt()

        # 模糊匹配
        candidates = fuzzy_match_song(song_query)
        if not candidates:
            raise SystemExit(f"未匹配到歌曲：{song_query}")
        chosen = candidates[0] if isinstance(candidates[0], str) else candidates[0][0]
        logging.info(f"直打模式：选择歌曲：{chosen}")

        save_song(chosen)

        if hold_for_ready:
            _wait_until_ready(control_file, "直打模式：等待 GUI Ready 信号以开始识别首音...")
        # 进入首音监测与演奏
        wait_first_note()
        play_song()
    finally:
        if watcher:
            watcher.stop()
        if 'mnt' in globals() and mnt:
            try:
                mnt.stop()
            except Exception:
                pass
# ==== End of UI Control additions ====

def main():
    parser = argparse.ArgumentParser(description="AutoDori 脚本（增强版 CLI）。")
    parser.add_argument("--ui", action="store_true", help="启动 GUI 控制面板（仅拉起界面，不直接跑主流程）。")
    parser.add_argument("--server", type=str, default=None, help="指定服务器名称 (例如 'b服' 或 '日服')。")
    parser.add_argument("--difficulty", type=str, choices=["easy", "normal", "hard", "expert", "special"], default="hard", help="指定难度。")
    parser.add_argument("--livemode", type=str, choices=["freelive", "challengelive"], default="freelive", help="指定 Live 模式。")
    parser.add_argument("--liveboost", type=int, default=1, help="所需的最低体力值。")
    parser.add_argument("--debug", action="store_true", help="启用调试模式（对于 UI，提供可视化反馈）。")
    parser.add_argument("--skip-version-check", action="store_true", help="跳过检查新版本。")

    # 新增：直打/控制文件/就绪闸/截图
    parser.add_argument("--mode", type=str, choices=["pipeline", "direct"], default="pipeline", help="运行模式：pipeline=原流程；direct=直打模式。")
    parser.add_argument("--song", type=str, default=None, help="直打模式的歌曲（可模糊匹配）。")
    parser.add_argument("--hold-for-ready", action="store_true", help="直打模式：在进入首音识别前等待 GUI Ready 信号。")
    parser.add_argument("--control-file", type=str, default="data/ui_control.json", help="GUI 控制文件路径（首音延迟/手动补偿/Ready 等）。")
    parser.add_argument("--capture-screen", type=str, default=None, help="仅截屏到指定路径并退出。")

    args = parser.parse_args()

    # 兼容桥：确保旧代码若读取 args.skip 不会崩
    if not hasattr(args, "skip"):
        setattr(args, "skip", bool(getattr(args, "skip_version_check", False)))

    # 更新共享状态（供旧逻辑使用）
    app_state.config['server_name'] = args.server
    app_state.config['difficulty'] = args.difficulty
    app_state.config['livemode'] = args.livemode
    app_state.config['min_liveboost'] = args.liveboost
    app_state.config['debug'] = args.debug

    # 检查更新（除非显式跳过）
    if not args.skip_version_check:
        get_current_version()
        if current_version:
            check_update()

    # 截图模式：最小化初始化，截一次屏后退出
    if args.capture_screen:
        _capture_screen_to_file(args.capture_screen)
        return

    # UI 模式：仅拉起界面，不跑自动化
    if args.ui:
        try:
            # 直接调用 GUI 的 main()（autodori_gui.py 内部已定义）
            from autodori_gui import main as gui_main
            configure_log(app_state.log_queue)
            logging.info("正在启动 GUI 模式...")
            gui_main()
        except Exception as e:
            import traceback
            logging.error("启动 GUI 失败。")
            logging.error(traceback.format_exc())
            sys.exit(f"错误：GUI 启动失败：{e}")
        return

    # CLI 模式
    if args.mode == "direct":
        if not args.song:
            sys.exit("直打模式需要提供 --song。")
        _run_direct_mode(args.song, args.hold_for_ready, args.control_file)
    else:
        # 兼容旧版：走原始自动化线程
        configure_log()
        runner = AutomationRunner(app_state)
        runner.run()
        logging.info("CLI 运行完成。正在退出。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # 捕获 SystemExit 以防止 finally 块中的错误
        logging.info(f"程序以代码 {e.code} 退出。")
    finally:
        # 修复：使用 is_closed() 方法
        if 'db' in globals() and not db.is_closed():
            db.close()
            logging.info("数据库连接已关闭。")

