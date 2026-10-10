import re
import time

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("SupportCardsLevelUpAuto")
class SupportCardsLevelUpAuto(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        action = _SupportCardsLevelUp(context)
        try:
            logger.info("事件: 支援卡升级")
            if action.navigate():
                action.upgrade()
            action.home()
            return True
        except RuntimeError as error:
            logger.error(f"支援卡升级停止: {error}")
            return False


class _SupportCardsLevelUp:
    def __init__(self, context: Context):
        self.context = context
        self.controller = context.tasker.controller
        self.submitted = False

    def snap(self):
        if self.context.tasker.stopping:
            raise RuntimeError("用户停止了任务")
        image = self.controller.post_screencap().wait().get()
        if image is None or image.shape[:2] != (1280, 720):
            raise RuntimeError("截图尺寸需要为 720×1280")
        return image

    def recognize(self, name, image, roi=None):
        node = "SupportCardsLevelUp" + name
        override = {node: {"recognition": {"param": {"roi": roi}}}} if roi else {}
        result = self.context.run_recognition(node, image, override)
        return result if result and result.hit else None

    def match(self, name, image=None):
        if image is None:
            image = self.snap()
        result = self.recognize(name, image)
        return result.box if result else None

    def ocr(self, name, roi, image=None):
        if image is None:
            image = self.snap()
        result = self.recognize(name, image, roi)
        return [item for item in result.filtered_results if item.score >= 0.85] if result else []

    def text(self, name, roi, image=None):
        return " ".join(item.text for item in self.ocr(name, roi, image))

    def click(self, x, y):
        if self.context.tasker.stopping:
            raise RuntimeError("用户停止了任务")
        if not self.controller.post_click(x, y).wait().succeeded:
            raise RuntimeError("点击失败")
        time.sleep(1.4)

    def click_box(self, box):
        self.click(box[0] + box[2] // 2, box[1] + box[3] // 2)

    def wait_template(self, name, timeout=45):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            box = self.match(name)
            if box:
                return box
            time.sleep(0.5)
        raise RuntimeError("未识别界面按钮: " + name)

    def tap_template(self, name):
        box = self.wait_template(name)
        self.click(box.x + box.w // 2, box.y + box.h // 2)

    def home(self):
        deadline = time.monotonic() + 180
        while True:
            image = self.snap()
            if self.match("HomeIdol", image):
                return
            if "LOADING" not in self.text("Text", [480, 1180, 240, 100], image):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("游戏加载超时")
            time.sleep(1)
        cancel = self.ocr("Cancel", [70, 1100, 285, 170])
        if cancel:
            self.click_box(cancel[0].box)
        self.tap_template("HomeIcon")
        self.wait_template("HomeIdol", timeout=180)

    def current_level(self, image):
        text = self.text("CurrentLevel", [0, 180, 350, 290], image)
        matches = re.findall(r"[Ll][Vv]\s*(\d+)\s*/\s*(\d+)", text)
        return tuple(map(int, matches[0])) if len(matches) == 1 else None

    def navigate(self):
        if not self.match("ListDetail"):
            self.home()
            self.tap_template("HomeIdol")
            self.tap_template("IdolSupport")
            self.wait_template("ListDetail")
        self.click(520, 1003)
        image = self.snap()
        if not self.ocr("SortTitle", [0, 0, 720, 400], image):
            raise RuntimeError("未识别排序窗口")
        levels = self.ocr("SortLevel", [105, 270, 100, 70], image)
        if len(levels) != 1:
            raise RuntimeError("无法确认等级排序选项")
        self.click_box(levels[0].box)
        decide = self.ocr("Decide", [350, 1050, 330, 220])
        if len(decide) != 1:
            raise RuntimeError("未识别排序确认按钮")
        self.click_box(decide[0].box)
        self.wait_template("ListDetail")
        if not self.match("Ascending"):
            self.click(650, 1003)
            self.wait_template("Ascending")
        # Retain filters. Only inspect fully visible rows, never scroll indefinitely.
        for y in range(507, 950, 123):
            for x in (140, 360, 580):
                self.wait_template("ListDetail")
                self.click(x, y)
                image = self.snap()
                in_detail = bool(self.match("DetailUpgrade", image))
                level = self.current_level(image)
                if level and 0 < level[0] < level[1] <= 100:
                    logger.info(f"找到可升级的支援卡: Lv{level[0]}/{level[1]}")
                    if not in_detail:
                        self.tap_template("ListDetail")
                    self.tap_template("DetailUpgrade")
                    return True
                if in_detail:
                    self.wait_template("HomeIcon")
                    self.click(297, 1220)
        logger.info("当前可见列表中没有可升级的支援卡，跳过")
        return False

    @staticmethod
    def single_level(text):
        match = re.fullmatch(r"\s*[Ll][Vv]\s*(\d+)\s*→\s*[Ll][Vv]\s*(\d+)\s*", text)
        if match and int(match[2]) == int(match[1]) + 1:
            return tuple(map(int, match.groups()))
        return None

    def upgrade(self):
        if self.submitted:
            raise RuntimeError("本次调用已提交升级，不再重复提交")
        image = self.snap()
        level = self.single_level(self.text("LevelChange", [230, 895, 250, 125], image))
        current = self.current_level(image)
        if not level or not current or current[0] != level[0] or level[1] > current[1]:
            raise RuntimeError("无法确认目标等级恰好增加一级")
        confirm = self.match("ConfirmUpgrade", image)
        if not confirm:
            logger.info("材料不足或强化按钮不可用，跳过")
            return
        have = self.text("Balance", [180, 1015, 200, 90], image).replace(",", "")
        after = self.text("Balance", [435, 1015, 185, 90], image).replace(",", "")
        cost = self.text("Cost", [565, 990, 100, 85], image).replace("−", "-")
        if not (have.isdigit() and after.isdigit() and re.fullmatch(r"-\d+", cost)):
            raise RuntimeError("无法确认强化 Pt 数量")
        cost = -int(cost)
        if int(have) < cost:
            logger.info("强化 Pt 不足，跳过")
            return
        if cost <= 0 or int(have) - int(after) != cost:
            raise RuntimeError("强化 Pt 变化不一致")
        self.submitted = True
        logger.info(f"提交支援卡升级: Lv{level[0]} → Lv{level[1]}，消耗 {cost} Pt")
        self.click(confirm.x + confirm.w // 2, confirm.y + confirm.h // 2)
        end = time.monotonic() + 45
        while time.monotonic() < end:
            image = self.snap()
            current = self.current_level(image)
            if current and current[0] == level[1] and self.match("DetailUpgrade", image):
                logger.info("支援卡升级成功")
                return
            time.sleep(1)
        raise RuntimeError("已提交升级，但未确认结果；请检查游戏，本次调用不会再次提交")
