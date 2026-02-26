import os
import re
import json
import time
import zipfile
import shutil
import requests
import subprocess
import threading
import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor, as_completed

# Настройки GUI
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")

# Константы
AI_DIR = "AI"
MODS_DIR = "mods"
QUESTS_DIR = os.path.join("config", "ftbquests", "quests")
CACHE_FILE = "cache.json"
KOBOLD_API = "http://localhost:5001/v1/chat/completions"

FORMAT_PATTERN = re.compile(r'(\$\([^)]+\)|§[0-9a-fk-orlmn]|\&[0-9a-fk-orlmn]|<br>|\n|%[0-9]*\$?[a-zA-Z\.])')
KEYS_TO_TRANSLATE = {"name", "title", "text", "description", "subtitle"}

IGNORE_TERMS = [
    "RF", "FE", "EU", "J", "mB", "mB/t", "RF/t", "FE/t", "AE", 
    "GUI", "UI", "HUD", "JEI", "REI", "EMI", "API", "JSON", "NBT",
    "FPS", "TPS", "HP", "XP", "MP", "XP/t", "XYZ", "RGB", "ID",
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"
]
IGNORE_TERMS.sort(key=len, reverse=True)
_escaped_terms = [re.escape(t) for t in IGNORE_TERMS]
IGNORE_PATTERN = re.compile(r'(?<![a-zA-Z])(' + '|'.join(_escaped_terms) + r')(?![a-zA-Z])')

# Глобальный кэш
translation_cache = {}

def load_cache():
    global translation_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                translation_cache = json.load(f)
        except:
            translation_cache = {}

def save_cache():
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(translation_cache, f, ensure_ascii=False, indent=2)

def get_mod_name(filepath):
    return os.path.basename(filepath).replace('.jar', '').split('-0')[0].split('-1')[0].replace('_', ' ').title()

def is_translation_key(text):
    t = text.strip()
    if not t or ' ' in t or '\n' in t: return False
    return bool(re.match(r'^[a-zA-Z0-9_-]+[.:][a-zA-Z0-9_.-]+$', t))

def load_lenient_json(raw_bytes):
    text = raw_bytes.decode('utf-8', errors='ignore')
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL) 
    text = re.sub(r'(?<!:)//.*', '', text) 
    text = re.sub(r',\s*}', '}', text) 
    text = re.sub(r',\s*]', ']', text)
    return json.loads(text, strict=False)

def extract_book_strings(data):
    strings = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in KEYS_TO_TRANSLATE and isinstance(v, str): strings.append(v)
            elif k in KEYS_TO_TRANSLATE and isinstance(v, list) and all(isinstance(i, str) for i in v): strings.extend(v)
            elif isinstance(v, (dict, list)): strings.extend(extract_book_strings(v))
    elif isinstance(data, list):
        for item in data: strings.extend(extract_book_strings(item))
    return strings

def inject_book_strings(data, t_iter):
    if isinstance(data, dict):
        for k, v in data.items():
            if k in KEYS_TO_TRANSLATE and isinstance(v, str): data[k] = next(t_iter)
            elif k in KEYS_TO_TRANSLATE and isinstance(v, list) and all(isinstance(i, str) for i in v): data[k] = [next(t_iter) for _ in v]
            elif isinstance(v, (dict, list)): inject_book_strings(v, t_iter)
    elif isinstance(data, list):
        for item in data: inject_book_strings(item, t_iter)

class TranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("MineAI Translator 2.0 (Ultimate Localizer)")
        self.geometry("900x700")
        self.resizable(False, False)
        
        self.ai_process = None
        load_cache()
        self.build_ui()

    def build_ui(self):
        # Левая панель (Настройки)
        self.frame_left = ctk.CTkFrame(self, width=300)
        self.frame_left.pack(side="left", fill="y", padx=10, pady=10)
        
        ctk.CTkLabel(self.frame_left, text="ЧТО ПЕРЕВОДИМ?", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(10, 5))
        self.var_mods = ctk.BooleanVar(value=True)
        self.var_books = ctk.BooleanVar(value=True)
        self.var_quests = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.frame_left, text="Интерфейс (Моды)", variable=self.var_mods).pack(anchor="w", padx=20, pady=5)
        ctk.CTkCheckBox(self.frame_left, text="Справочники (Книги)", variable=self.var_books).pack(anchor="w", padx=20, pady=5)
        ctk.CTkCheckBox(self.frame_left, text="Квесты (FTB Quests)", variable=self.var_quests).pack(anchor="w", padx=20, pady=5)

        ctk.CTkLabel(self.frame_left, text="ДВИЖОК ПЕРЕВОДА", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(20, 5))
        self.var_engine = ctk.StringVar(value="google")
        ctk.CTkRadioButton(self.frame_left, text="Google (Быстро, ИИ-потоки)", variable=self.var_engine, value="google").pack(anchor="w", padx=20, pady=5)
        ctk.CTkRadioButton(self.frame_left, text="Локальная Нейросеть (Лор)", variable=self.var_engine, value="ai").pack(anchor="w", padx=20, pady=5)

        ctk.CTkLabel(self.frame_left, text="РЕЖИМ", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(20, 5))
        self.var_mode = ctk.StringVar(value="append")
        ctk.CTkRadioButton(self.frame_left, text="Доперевод (Сохранить старое)", variable=self.var_mode, value="append").pack(anchor="w", padx=20, pady=5)
        ctk.CTkRadioButton(self.frame_left, text="Пропуск (От 90% готовности)", variable=self.var_mode, value="skip").pack(anchor="w", padx=20, pady=5)
        ctk.CTkRadioButton(self.frame_left, text="Полная перезапись (С нуля)", variable=self.var_mode, value="force").pack(anchor="w", padx=20, pady=5)

        self.btn_analyze = ctk.CTkButton(self.frame_left, text="Анализ сборки", fg_color="#0066cc", hover_color="#004c99", command=self.start_analysis)
        self.btn_analyze.pack(pady=(30, 10), fill="x", padx=20)
        
        self.btn_start = ctk.CTkButton(self.frame_left, text="НАЧАТЬ ПЕРЕВОД", fg_color="#28a745", hover_color="#218838", height=40, font=ctk.CTkFont(weight="bold"), command=self.start_translation)
        self.btn_start.pack(pady=10, fill="x", padx=20)

        # Правая панель (Консоль)
        self.frame_right = ctk.CTkFrame(self)
        self.frame_right.pack(side="right", fill="both", expand=True, padx=(0, 10), pady=10)
        
        self.textbox = ctk.CTkTextbox(self.frame_right, state="disabled", font=ctk.CTkFont(family="Consolas", size=12))
        self.textbox.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.progress_bar = ctk.CTkProgressBar(self.frame_right)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 5))
        self.progress_bar.set(0)
        
        self.lbl_status = ctk.CTkLabel(self.frame_right, text="Ожидание действий...")
        self.lbl_status.pack(pady=(0, 10))

    def log(self, message):
        self.textbox.configure(state="normal")
        self.textbox.insert("end", message + "\n")
        self.textbox.see("end")
        self.textbox.configure(state="disabled")
        
    def set_status(self, text, val=None):
        self.lbl_status.configure(text=text)
        if val is not None:
            self.progress_bar.set(val)
            
    def lock_ui(self, lock=True):
        state = "disabled" if lock else "normal"
        self.btn_analyze.configure(state=state)
        self.btn_start.configure(state=state)

    def start_analysis(self):
        self.lock_ui(True)
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        threading.Thread(target=self.run_analysis, daemon=True).start()

    def start_translation(self):
        self.lock_ui(True)
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        threading.Thread(target=self.run_translation, daemon=True).start()

    # ================= ЛОГИКА АНАЛИЗА =================
    def run_analysis(self):
        self.log("🚀 Запуск сканирования сборки...\n")
        total_en, total_ru = 0, 0
        
        jar_files = [os.path.join(MODS_DIR, f) for f in os.listdir(MODS_DIR) if f.endswith('.jar')] if os.path.exists(MODS_DIR) else []
        for i, filepath in enumerate(jar_files):
            mod_name = get_mod_name(filepath)
            self.set_status(f"Анализ мода: {mod_name}...", i / len(jar_files))
            try:
                with zipfile.ZipFile(filepath, 'r') as zin:
                    ru_files = {item.filename.lower(): item for item in zin.infolist() if 'ru_ru.json' in item.filename.lower() or '/ru_ru/' in item.filename.lower()}
                    for item in zin.infolist():
                        if item.filename.lower().endswith('en_us.json') and 'patchouli' not in item.filename.lower():
                            try:
                                en_data = load_lenient_json(zin.read(item))
                                ru_t = item.filename.lower().replace('en_us.json', 'ru_ru.json')
                                ru_data = load_lenient_json(zin.read(ru_files[ru_t])) if ru_t in ru_files else {}
                                en_c = len([k for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v)])
                                ru_c = sum(1 for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v) and (re.search(r'[А-Яа-яЁё]', str(ru_data.get(k,""))) or ru_data.get(k,"") != v))
                                if en_c > 0:
                                    total_en += en_c; total_ru += ru_c
                                    self.log(f"📦 {mod_name} [Интерфейс]: {ru_c}/{en_c} ({int(ru_c/en_c*100)}%)")
                            except: pass
            except: pass

        snbt_files = []
        if os.path.exists(QUESTS_DIR):
            for root, _, files in os.walk(QUESTS_DIR):
                snbt_files.extend([os.path.join(root, f) for f in files if f.endswith('.snbt')])
                
        for i, filepath in enumerate(snbt_files):
            self.set_status(f"Анализ квеста: {os.path.basename(filepath)}...", i / len(snbt_files))
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                strings = re.findall(r'(?:"|)(?:title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.IGNORECASE)
                desc_blocks = re.findall(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', content, re.DOTALL | re.IGNORECASE)
                for b in desc_blocks: strings.extend(re.findall(r'"((?:[^"\\]|\\.)*)"', b))
                
                valid_str = list(set([s for s in strings if s.strip() and not is_translation_key(s) and re.search(r'[a-zA-Z]', s)]))
                en_c = len(valid_str)
                ru_c = sum(1 for s in valid_str if re.search(r'[А-Яа-яЁё]', s))
                if en_c > 0:
                    total_en += en_c; total_ru += ru_c
                    self.log(f"📜 {os.path.basename(filepath)} [Квесты]: {ru_c}/{en_c} ({int(ru_c/en_c*100)}%)")
            except: pass

        if total_en > 0:
            pct = int((total_ru / total_en) * 100)
            self.log(f"\n✅ АНАЛИЗ ЗАВЕРШЕН!\nОбщая готовность: {pct}%\nСтрок всего: {total_en} | Переведено: {total_ru}")
        else:
            self.log("\n❌ Не найдено файлов для перевода!")
            
        self.set_status("Готово", 1.0)
        self.lock_ui(False)

    # ================= ЛОГИКА ПЕРЕВОДА =================
    def run_translation(self):
        modes = []
        if self.var_mods.get(): modes.append("mods")
        if self.var_books.get(): modes.append("books")
        if self.var_quests.get(): modes.append("quests")
        
        engine = self.var_engine.get()
        mode_overwrite = self.var_mode.get()

        jar_files = [os.path.join(MODS_DIR, f) for f in os.listdir(MODS_DIR) if f.endswith('.jar')] if os.path.exists(MODS_DIR) else []
        snbt_files = []
        if os.path.exists(QUESTS_DIR):
            for root, _, files in os.walk(QUESTS_DIR):
                snbt_files.extend([os.path.join(root, f) for f in files if f.endswith('.snbt')])

        total_files = len(jar_files) + len(snbt_files)
        if total_files == 0:
            self.log("❌ Файлы не найдены! Положите скрипт в папку с игрой.")
            self.lock_ui(False)
            return

        if engine == "ai" and not self.setup_and_start_ai():
            self.lock_ui(False)
            return

        self.log("🚀 ЗАПУСК ПЕРЕВОДА...\n")
        
        processed = 0
        for filepath in jar_files:
            self.process_jar(filepath, modes, engine, mode_overwrite)
            processed += 1
            self.set_status(f"Обработано файлов: {processed}/{total_files}", processed / total_files)
            
        for filepath in snbt_files:
            if "quests" in modes:
                self.process_snbt(filepath, engine, mode_overwrite)
            processed += 1
            self.set_status(f"Обработано файлов: {processed}/{total_files}", processed / total_files)

        save_cache()
        self.log("\n✅ ГЛОБАЛЬНЫЙ ПЕРЕВОД УСПЕШНО ЗАВЕРШЕН!")
        self.set_status("Все задачи выполнены!", 1.0)
        if self.ai_process:
            self.ai_process.terminate()
        self.lock_ui(False)

    def setup_and_start_ai(self):
        if not os.path.exists(AI_DIR): os.makedirs(AI_DIR)
        models = [f for f in os.listdir(AI_DIR) if f.endswith('.gguf')]
        if not models:
            self.log("❌ Ошибка: В папке AI нет .gguf модели!")
            return False
            
        self.log(f"🤖 Запуск ИИ: {models[0]}...")
        self.ai_process = subprocess.Popen([os.path.join(AI_DIR, "koboldcpp.exe"), os.path.join(AI_DIR, models[0]), "--port", "5001", "--quiet"], stdout=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if requests.get(KOBOLD_API.replace("chat/completions", "models"), timeout=1).status_code == 200:
                    self.log("✅ ИИ успешно запущен!\n")
                    return True
            except: time.sleep(1)
        self.log("❌ Ошибка: Сервер ИИ не отвечает.")
        return False

    def translate_engine(self, data_dict, engine):
        """Отправляет словари на перевод, используя кэш и потоки"""
        keys = list(data_dict.keys())
        result = {}
        to_translate = {}
        
        # Проверка кэша и маскировка
        for k in keys:
            text = data_dict[k]
            if text in translation_cache:
                result[k] = translation_cache[text]
                continue
                
            mapping = {}
            def mask_format(m):
                marker = f" [#{len(mapping)}#] "
                mapping[marker.strip()] = m.group(0)
                return marker
                
            masked = FORMAT_PATTERN.sub(mask_format, text)
            masked = IGNORE_PATTERN.sub(mask_format, masked)
            masked = re.sub(r'\s+', ' ', masked).strip()
            
            if not masked:
                result[k] = text
                continue
                
            to_translate[k] = {"original": text, "masked": masked, "mapping": mapping}

        if not to_translate: return result

        # Перевод через Google (Многопоточность)
        if engine == "google":
            chunks = []
            curr_keys, curr_text = [], ""
            for k, val in to_translate.items():
                if len(curr_text) + len(val["masked"]) > 2000 or len(curr_keys) >= 20:
                    chunks.append((curr_keys, curr_text))
                    curr_keys, curr_text = [k], val["masked"]
                else:
                    curr_keys.append(k)
                    curr_text = curr_text + " |~| " + val["masked"] if curr_text else val["masked"]
            if curr_keys: chunks.append((curr_keys, curr_text))

            def translate_chunk(chunk_keys, text_to_send):
                for _ in range(3):
                    try:
                        res = requests.get("https://translate.googleapis.com/translate_a/single", params={"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": text_to_send}, timeout=10)
                        if res.status_code == 429: time.sleep(3); continue
                        parts = re.split(r'\s*\|\s*~\s*\|\s*', "".join([p[0] for p in res.json()[0] if p[0]]))
                        if len(parts) == len(chunk_keys):
                            return chunk_keys, parts
                    except: time.sleep(1)
                return chunk_keys, None # Если сломалось - вернем None

            with ThreadPoolExecutor(max_workers=3) as executor: # БЕЗОПАСНЫЙ ЛИМИТ ДЛЯ GOOGLE
                futures = [executor.submit(translate_chunk, ck, txt) for ck, txt in chunks]
                for future in as_completed(futures):
                    c_keys, c_parts = future.result()
                    if c_parts:
                        for idx, k in enumerate(c_keys):
                            trans = c_parts[idx].strip()
                            for m_idx, (m, orig) in enumerate(to_translate[k]["mapping"].items()):
                                trans = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, trans)
                            result[k] = trans
                            translation_cache[to_translate[k]["original"]] = trans # Сохраняем в кэш
                            self.log(f" {to_translate[k]['original'][:30]} -> {trans[:30]}")
                    else:
                        # Запасной поштучный план
                        for k in c_keys:
                            try:
                                res = requests.get("https://translate.googleapis.com/translate_a/single", params={"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": to_translate[k]["masked"]}, timeout=5).json()
                                trans = "".join([p[0] for p in res[0] if p[0]])
                                for m_idx, (m, orig) in enumerate(to_translate[k]["mapping"].items()):
                                    trans = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, trans)
                                result[k] = trans
                                translation_cache[to_translate[k]["original"]] = trans
                                self.log(f" {to_translate[k]['original'][:30]} -> {trans[:30]}")
                            except: result[k] = to_translate[k]["original"]
                            time.sleep(0.3)
                            
        # Перевод через ИИ (Однопоточно, чтобы не взорвать ПК)
        else:
            batch_keys = list(to_translate.keys())
            for i in range(0, len(batch_keys), 20):
                b_keys = batch_keys[i:i+20]
                b_dict = {k: to_translate[k]["masked"] for k in b_keys}
                prompt = f"Ты локализатор. Переведи JSON. ПРАВИЛА: Не переводи ключи. Сохраняй [#0#]. Текст: {json.dumps(b_dict, ensure_ascii=False)}"
                try:
                    res = requests.post(KOBOLD_API, json={"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 2048}, timeout=120).json()
                    trans_text = re.sub(r'^```json\s*|^```\s*|```$', '', res['choices'][0]['message']['content'].strip(), flags=re.IGNORECASE).strip()
                    trans_dict = json.loads(trans_text, strict=False)
                    for k in b_keys:
                        if k in trans_dict:
                            trans = trans_dict[k]
                            for m_idx, (m, orig) in enumerate(to_translate[k]["mapping"].items()):
                                trans = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, trans)
                            result[k] = trans
                            translation_cache[to_translate[k]["original"]] = trans
                            self.log(f" {to_translate[k]['original'][:30]} -> {trans[:30]}")
                        else: result[k] = to_translate[k]["original"]
                except:
                    for k in b_keys: result[k] = to_translate[k]["original"]

        # Сохраняем кэш каждые 100 переводов (на всякий случай)
        if len(translation_cache) % 50 == 0: save_cache()
        return result

    def process_jar(self, filepath, modes, engine, mode_overwrite):
        mod_name = get_mod_name(filepath)
        temp_filepath = filepath + ".temp"
        translated_any = False
        
        try:
            with zipfile.ZipFile(filepath, 'r') as zin, zipfile.ZipFile(temp_filepath, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                ru_files_written = set()
                ru_files = {item.filename.lower(): item for item in zin.infolist() if 'ru_ru.json' in item.filename.lower() or '/ru_ru/' in item.filename.lower()}

                for item in zin.infolist():
                    f_lower = item.filename.lower()
                    is_book = ('/en_us/' in f_lower and f_lower.endswith('.json') and ('patchouli' in f_lower or 'lexicon' in f_lower or 'guide' in f_lower))
                    is_lang = (f_lower.endswith('en_us.json') and not is_book)
                    if 'ru_ru.json' not in f_lower and '/ru_ru/' not in f_lower:
                        zout.writestr(item, zin.read(item))

                    if "mods" in modes and is_lang:
                        ru_filename = re.sub(r'en_us\.json$', 'ru_ru.json', item.filename, flags=re.IGNORECASE)
                        ru_t = ru_filename.lower()
                        en_data = load_lenient_json(zin.read(item))
                        ru_data = load_lenient_json(zin.read(ru_files[ru_t])) if ru_t in ru_files else {}
                        
                        final_data = en_data.copy()
                        keys_to_translate = {}
                        
                        for k, en_text in en_data.items():
                            if not isinstance(en_text, str) or not en_text.strip(): continue
                            if mode_overwrite == "append" and k in ru_data and isinstance(ru_data[k], str) and ru_data[k].strip():
                                final_data[k] = ru_data[k]
                                if final_data[k] == en_text and re.search(r'[a-zA-Z]', en_text): keys_to_translate[k] = en_text
                            elif re.search(r'[a-zA-Z]', en_text): keys_to_translate[k] = en_text

                        total_en = len([k for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v)])
                        if total_en > 0:
                            if mode_overwrite == "skip" and (total_en - len(keys_to_translate)) >= total_en * 0.9:
                                self.log(f"⏩ {mod_name} (Интерфейс): Пропуск (уже переведен)")
                                if ru_t in ru_files:
                                    zout.writestr(zin.getinfo(ru_files[ru_t].filename), zin.read(ru_files[ru_t]))
                                    ru_files_written.add(ru_files[ru_t].filename)
                            elif len(keys_to_translate) == 0 and mode_overwrite == "append":
                                zout.writestr(ru_filename, json.dumps(final_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                ru_files_written.add(ru_filename)
                                translated_any = True
                            else:
                                self.log(f"⚡ Перевод {mod_name} (Интерфейс) - {len(keys_to_translate)} строк")
                                trans_dict = self.translate_engine(keys_to_translate, engine)
                                for k, v in trans_dict.items(): final_data[k] = v
                                zout.writestr(ru_filename, json.dumps(final_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                ru_files_written.add(ru_filename)
                                translated_any = True

                    elif "books" in modes and is_book:
                        ru_filename = re.sub(r'/en_us/', '/ru_ru/', item.filename, flags=re.IGNORECASE)
                        ru_t = ru_filename.lower()
                        en_data = load_lenient_json(zin.read(item))
                        ru_data = load_lenient_json(zin.read(ru_files[ru_t])) if ru_t in ru_files else {}
                        
                        en_strings = [s for s in extract_book_strings(en_data) if s.strip()]
                        ru_strings = [s for s in extract_book_strings(ru_data) if s.strip()] if ru_data else []
                        
                        keys_to_translate = {}
                        final_strings = []
                        
                        for i, en_s in enumerate(en_strings):
                            if mode_overwrite == "append" and i < len(ru_strings) and ru_strings[i].strip():
                                final_strings.append(ru_strings[i])
                                if ru_strings[i] == en_s and re.search(r'[a-zA-Z]', en_s): keys_to_translate[str(i)] = en_s
                            else:
                                final_strings.append(en_s)
                                if re.search(r'[a-zA-Z]', en_s): keys_to_translate[str(i)] = en_s

                        total_en = len([s for s in en_strings if re.search(r'[a-zA-Z]', s)])
                        if total_en > 0:
                            if mode_overwrite == "skip" and (total_en - len(keys_to_translate)) >= total_en * 0.9:
                                self.log(f"⏩ {mod_name} (Книга): Пропуск (уже переведен)")
                                if ru_t in ru_files:
                                    zout.writestr(zin.getinfo(ru_files[ru_t].filename), zin.read(ru_files[ru_t]))
                                    ru_files_written.add(ru_files[ru_t].filename)
                            elif len(keys_to_translate) == 0 and mode_overwrite == "append":
                                inject_book_strings(en_data, iter(final_strings))
                                zout.writestr(ru_filename, json.dumps(en_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                ru_files_written.add(ru_filename)
                                translated_any = True
                            else:
                                self.log(f"⚡ Перевод {mod_name} (Книга) - {len(keys_to_translate)} строк")
                                trans_dict = self.translate_engine(keys_to_translate, engine)
                                for i in range(len(final_strings)):
                                    if str(i) in trans_dict: final_strings[i] = trans_dict[str(i)]
                                inject_book_strings(en_data, iter(final_strings))
                                zout.writestr(ru_filename, json.dumps(en_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                ru_files_written.add(ru_filename)
                                translated_any = True

                for item in zin.infolist():
                    if ('ru_ru.json' in item.filename.lower() or '/ru_ru/' in item.filename.lower()) and item.filename not in ru_files_written:
                        try: zout.writestr(item, zin.read(item))
                        except: pass

            if translated_any:
                shutil.move(temp_filepath, filepath)
            else: os.remove(temp_filepath)
        except Exception as e:
            if os.path.exists(temp_filepath): os.remove(temp_filepath)
            self.log(f"❌ Ошибка в {mod_name}: {e}")

    def process_snbt(self, filepath, engine, mode_overwrite):
        filename = os.path.basename(filepath)
        bak_path = filepath + ".bak"
        if not os.path.exists(bak_path): shutil.copy2(filepath, bak_path)
        content_path = filepath if mode_overwrite == "append" else bak_path
            
        try:
            with open(content_path, 'r', encoding='utf-8') as f: content = f.read()
                
            strings_to_translate = []
            for m in re.finditer(r'(?:"|)(title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.IGNORECASE):
                val = m.group(2)
                if val.strip() and not is_translation_key(val) and re.search(r'[a-zA-Z]', val): 
                    if mode_overwrite == "append" and re.search(r'[А-Яа-яЁё]', val): continue
                    strings_to_translate.append(val)
                
            for m in re.finditer(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', content, re.DOTALL | re.IGNORECASE):
                for str_m in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(1)):
                    val = str_m.group(1)
                    if val.strip() and not is_translation_key(val) and re.search(r'[a-zA-Z]', val): 
                        if mode_overwrite == "append" and re.search(r'[А-Яа-яЁё]', val): continue
                        strings_to_translate.append(val)
                    
            strings_to_translate = list(set(strings_to_translate))
            
            if len(strings_to_translate) == 0:
                if mode_overwrite == "append": self.log(f"⏩ {filename} (Квесты): Полностью допереведен")
                return
                
            if mode_overwrite == "skip":
                with open(filepath, 'r', encoding='utf-8') as f:
                    if re.search(r'[А-Яа-яЁё]', f.read()):
                        self.log(f"⏩ {filename} (Квесты): Пропуск (перевод готов)")
                        return

            self.log(f"⚡ Перевод квестов {filename} - {len(strings_to_translate)} строк")
            
            chunk_dict = {str(i): val for i, val in enumerate(strings_to_translate)}
            trans_dict = self.translate_engine(chunk_dict, engine)
            trans_map = {strings_to_translate[i]: trans_dict.get(str(i), strings_to_translate[i]) for i in range(len(strings_to_translate))}
            
            def repl_single(m):
                key, val = m.group(1), m.group(2)
                new_val = trans_map.get(val, val).replace('\\"', '"').replace('"', '\\"')
                return f'{key}: "{new_val}"'
                
            content = re.sub(r'(?:"|)(title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', repl_single, content, flags=re.IGNORECASE)
            
            def repl_desc(m):
                def repl_inner(str_m):
                    val = str_m.group(1)
                    new_val = trans_map.get(val, val).replace('\\"', '"').replace('"', '\\"')
                    return f'"{new_val}"'
                new_desc_content = re.sub(r'"((?:[^"\\]|\\.)*)"', repl_inner, m.group(1))
                return f'description: [{new_desc_content}]'
                
            content = re.sub(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', repl_desc, content, flags=re.DOTALL | re.IGNORECASE)
            
            with open(filepath, 'w', encoding='utf-8') as f: f.write(content)
        except Exception as e: self.log(f"❌ Ошибка квеста {filename}: {e}")

if __name__ == '__main__':
    app = TranslatorApp()
    app.mainloop()
