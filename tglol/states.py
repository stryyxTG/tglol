from aiogram.fsm.state import State, StatesGroup


class AddByCode(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_twofa = State()


class AddBySession(StatesGroup):
    waiting_session = State()
    waiting_json = State()


class AddByZip(StatesGroup):
    waiting_zip = State()


class CreateWorker(StatesGroup):
    waiting_telegram_id = State()
    waiting_name = State()


class RenameWorker(StatesGroup):
    waiting_name = State()


class AddProxy(StatesGroup):
    waiting_text = State()


class AssignAccount(StatesGroup):
    waiting_worker_id = State()
    waiting_amount = State()


class AssignProxy(StatesGroup):
    waiting_worker_id = State()
    waiting_amount = State()
