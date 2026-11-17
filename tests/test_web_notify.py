"""web_notify helpers with fakes; the Telegram chat itself is in test_telegram_chat."""
import threading

from igris import web_notify


def test_channel_settings_keep_only_set_values():
    values = {"TELEGRAM_BOT_TOKEN": "t", "NTFY_TOPIC": ""}
    assert web_notify.channel_settings(values.get) == {"IGRIS_TELEGRAM_BOT_TOKEN": "t"}


def test_pump_answers_survives_failures_and_stops():
    stop = threading.Event()
    calls = []

    class Flow:
        def pump(self):
            calls.append("pump")
            if len(calls) == 1:
                raise RuntimeError("network down")

    def tick():
        calls.append("tick")
        if calls.count("tick") == 2:
            stop.set()

    waits = iter([False, False, True])
    stop.wait = lambda interval: next(waits)
    web_notify.pump_answers(Flow(), stop, interval=0, tick=tick)
    assert calls == ["pump", "tick", "pump", "tick"]
