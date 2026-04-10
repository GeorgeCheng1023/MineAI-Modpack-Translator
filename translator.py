import os
import re
import json
import time
import zipfile
import shutil
import requests
import subprocess
import threading
import traceback
import configparser
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

# ================= НАСТРОЙКИ (settings.ini) =================
config = configparser.ConfigParser()
settings_file = "settings.ini"

if not os.path.exists(settings_file):
    config["GENERAL"] = {
        "ai_dir": "AI", 
        "theme": "Dark", 
        "color": "green",
        "google_workers": "5",
        "ui_language": "ru"
    }
    with open(settings_file, "w", encoding="utf-8") as f:
        config.write(f)

config.read(settings_file, encoding="utf-8")
AI_DIR = config.get("GENERAL", "ai_dir", fallback="AI")
APP_THEME = config.get("GENERAL", "theme", fallback="Dark")
APP_COLOR = config.get("GENERAL", "color", fallback="green")
GOOGLE_WORKERS = config.getint("GENERAL", "google_workers", fallback=5)
UI_LANG = config.get("GENERAL", "ui_language", fallback="ru")

ctk.set_appearance_mode(APP_THEME)
ctk.set_default_color_theme(APP_COLOR)

# Константы
CACHE_FILE_STD = "cache.json"      
CACHE_FILE_AI = "ai_cache.json"    
KOBOLD_API = "http://localhost:5001/v1/chat/completions"
DICT_FILE = "dictionary.json"
UI_I18N_FILE = "ui_i18n.json"

# ================= УСИЛЕННЫЙ ТИТАНОВЫЙ ЩИТ ИЗ ТЕСТЕРА =================
FORMAT_PATTERN = re.compile(
    r'('
    r'\$\([^)]+\)|'                 # Макросы $(...)
    r'[&§][0-9a-fk-orlmn]|'         # Цвета
    r'<[^>]+>|'                     # Теги <item...>
    r'\{[^\}]+\}|'                  # JSON
    r'\]\([^)]+\)|'                 # Полные ссылки ](url)
    r'\[[a-z0-9_.-]+:[a-z0-9_./-]+\]|' # [ae2:item]
    r'\([a-z0-9_.-]+:[a-z0-9_./-]+\)|' # (ae2:item)
    r'\([A-Za-z0-9_./-]+\.md[#a-zA-Z0-9_-]*\)|' # Ссылки (.md)
    r'\n|'                          # Переносы
    r'%[0-9.,]*\$?[a-zA-Z%]'        # Переменные форматирования Minecraft
    r')', flags=re.IGNORECASE
)

KEYS_TO_TRANSLATE = {"name", "title", "text", "description", "subtitle"}

IGNORE_TERMS = [
    "RF", "FE", "EU", "J", "mB", "mB/t", "RF/t", "FE/t", "AE", "kW", "kRF", "mB/tick", "ticks",
    "GUI", "UI", "HUD", "JEI", "REI", "EMI", "API", "JSON", "NBT",
    "FPS", "TPS", "HP", "XP", "MP", "XP/t", "XYZ", "RGB", "ID",
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"
]
IGNORE_TERMS.sort(key=len, reverse=True)
_escaped_terms = [re.escape(t) for t in IGNORE_TERMS]
IGNORE_PATTERN = re.compile(r'(?<![a-zA-Z])(' + '|'.join(_escaped_terms) + r')(?![a-zA-Z])')

# ================= ВНЕШНИЙ СЛОВАРЬ АВТОЗАМЕН =================
DEFAULT_DICT = {
    "полуслой": "плита",
    "полуслои": "плиты",
    "полукирпич": "плита",
    "полукирпичи": "плиты",
    "сыромятная медь": "сырая медь",
    "сыромятного меди": "сырой меди",
    "сыромятное железо": "сырое железо",
    "сыромятного железа": "сырого железа",
    "сыромятное золото": "сырое золото",
    "сыромятного золота": "сырого золота",
    "необтanium": "необтаниум",
    "доместик": "прирученный",
    "wereld": "мир"
}

def load_dictionary():
    if not os.path.exists(DICT_FILE):
        with open(DICT_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_DICT, f, ensure_ascii=False, indent=4)
        return DEFAULT_DICT
    try:
        with open(DICT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return DEFAULT_DICT

TERMINOLOGY_FIXES = load_dictionary()

def fix_formatting(text):
    if not text: return text
    text = re.sub(r'([&§][0-9a-fk-or])\s+', r'\1', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+([&§][r])', r'\1', text, flags=re.IGNORECASE)
    text = re.sub(r'\[\s+(%\d*\$?[sd])\s+\]', r'[\1]', text)
    text = re.sub(r'\(\s+(%\d*\$?[sd])\s+\)', r'(\1)', text)
    text = re.sub(r'\"\s+(%\d*\$?[sd])\s+\"', r'"\1"', text)
    text = re.sub(r'\s+,', ',', text)
    text = re.sub(r'\s+:', ':', text)
    
    # ФИКС КРАШЕЙ КНОПОК И ТУЛТИПОВ МАЙНКРАФТА
    text = re.sub(r'%\s+([sd])', r'%\1', text)                       # "% s" -> "%s"
    text = re.sub(r'%\s+(\d+)\s*\$\s*([sd])', r'%\1$\2', text)       # "% 1 $ s" -> "%1$s"
    text = re.sub(r'%\s*\.\s*(\d+)\s*([fd])', r'%.\1\2', text)       # "% . 2 f" -> "%.2f"
    
    # ФИКС РАЗРЫВА ССЫЛОК MARKDOWN
    text = re.sub(r'\]\s+\(', '](', text)                            # "] (" -> "]("
    text = re.sub(r'!\s+\[', '![', text)                             # "! [" -> "!["
    text = re.sub(r'\[\s+', '[', text)                               # "[ " -> "["
    text = re.sub(r'\s+\]', ']', text)                               # " ]" -> "]"
    
    text = re.sub(r' {2,}', ' ', text)
    return text

def fix_terminology(text):
    if not text: return text
    for wrong, right in TERMINOLOGY_FIXES.items():
        def repl(match):
            word = match.group(0)
            if word.istitle(): return right.capitalize()
            elif word.isupper(): return right.upper()
            return right
        text = re.sub(r'\b' + wrong + r'\b', repl, text, flags=re.IGNORECASE)
    return text

def polish_translation(text):
    if not isinstance(text, str): return text
    return fix_terminology(fix_formatting(text))

# ================= ДВОЙНОЙ КЭШ =================
def load_and_polish_cache(filepath):
    cache_data = {}
    changes = 0
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            for k, v in list(cache_data.items()):
                new_v = polish_translation(v)
                if new_v != v:
                    cache_data[k] = new_v
                    changes += 1
            if changes > 0:
                save_cache_data(cache_data, filepath)
        except Exception:
            cache_data = {}
    return cache_data, changes

def save_cache_data(cache_data, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

LANGUAGES = {
    "Русский": {"file": "ru_ru", "api": "ru", "deepl": "RU", "name": "Russian", "regex": r'[А-Яа-яЁё]'},
    "English (UK)": {"file": "en_gb", "api": "en", "deepl": "EN-GB", "name": "English", "regex": r'[a-zA-Z]'},
    "Español": {"file": "es_es", "api": "es", "deepl": "ES", "name": "Spanish", "regex": r'[áéíóúüñÁÉÍÓÚÜÑ]'},
    "Deutsch": {"file": "de_de", "api": "de", "deepl": "DE", "name": "German", "regex": r'[äöüßÄÖÜẞ]'},
    "Français": {"file": "fr_fr", "api": "fr", "deepl": "FR", "name": "French", "regex": r'[àâæçéèêëîïôœùûüÿÀÂÆÇÉÈÊËÎÏÔŒÙÛÜŸ]'},
    "中文 (Упрощ.)": {"file": "zh_cn", "api": "zh-CN", "deepl": "ZH", "name": "Simplified Chinese", "regex": r'[\u4e00-\u9fff]'},
    "中文 (繁體)": {"file": "zh_tw", "api": "zh-TW", "deepl": "ZH", "name": "Traditional Chinese", "regex": r'[\u4e00-\u9fff]'},
    "日本語": {"file": "ja_jp", "api": "ja", "deepl": "JA", "name": "Japanese", "regex": r'[\u3040-\u30ff\u4e00-\u9fff]'},
    "Português": {"file": "pt_br", "api": "pt", "deepl": "PT-BR", "name": "Portuguese", "regex": r'[ãáâéêíóôõúçÃÁÂÉÊÍÓÔÕÚÇ]'},
    "Italiano": {"file": "it_it", "api": "it", "deepl": "IT", "name": "Italian", "regex": r'[àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ]'},
    "Polski": {"file": "pl_pl", "api": "pl", "deepl": "PL", "name": "Polish", "regex": r'[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]'},
    "한국어": {"file": "ko_kr", "api": "ko", "deepl": "KO", "name": "Korean", "regex": r'[\u3131-\uD79D]'},
    "Українська": {"file": "uk_ua", "api": "uk", "deepl": "UK", "name": "Ukrainian", "regex": r'[А-Яа-яІіЇїЄєҐґ]'},
    "Türkçe": {"file": "tr_tr", "api": "tr", "deepl": "TR", "name": "Turkish", "regex": r'[çğıöşüÇĞİÖŞÜ]'},
    "Čeština": {"file": "cs_cz", "api": "cs", "deepl": "CS", "name": "Czech", "regex": r'[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]'},
    "Nederlands": {"file": "nl_nl", "api": "nl", "deepl": "NL", "name": "Dutch", "regex": r'[a-zA-Z]'},
    "Română": {"file": "ro_ro", "api": "ro", "deepl": "RO", "name": "Romanian", "regex": r'[ăâîșşțţĂÂÎȘŞȚŢ]'}
}

def load_ui_i18n(filepath=UI_I18N_FILE):
    fallback_labels = {"ru": "Русский", "en": "English"}
    fallback_translations = {
        "ru": {"app_title": "MineAI Translator (AE2 Integrated Edition)"},
        "en": {"app_title": "MineAI Translator (AE2 Integrated Edition)"}
    }
    if not os.path.exists(filepath):
        return fallback_labels, fallback_translations
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        labels = data.get("language_labels", fallback_labels)
        translations = data.get("translations", fallback_translations)
        if "en" not in translations:
            translations["en"] = fallback_translations["en"]
        if "ru" not in translations:
            translations["ru"] = fallback_translations["ru"]
        if "en" not in labels:
            labels["en"] = "English"
        if "ru" not in labels:
            labels["ru"] = "Русский"
        return labels, translations
    except Exception:
        return fallback_labels, fallback_translations


UI_LANGUAGE_LABELS, UI_TRANSLATIONS = load_ui_i18n()

def get_mod_name(filepath):
    return os.path.basename(filepath).replace('.jar', '').split('-0')[0].split('-1')[0].replace('_', ' ').title()

def is_translation_key(text):
    t = text.strip()
    if not t or ' ' in t or '\n' in t: return False
    return bool(re.match(r'^[a-zA-Z0-9_-]+[.:][a-zA-Z0-9_.-]+$', t))

def load_lenient_json(raw_bytes):
    text = raw_bytes.decode('utf-8-sig', errors='ignore')
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL) 
    text = re.sub(r'(?m)^\s*//.*$', '', text) 
    text = re.sub(r',\s*([\]}])', r'\1', text) 
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

@lru_cache(maxsize=10000)
def is_technical_term(text):
    if not text: return True
    lower = text.lower()
    if not re.search(r'[a-z]', lower): return True 
    if re.match(r'^[a-z0-9_.-]+$', lower) and any(c in lower for c in '._'):
        return True
    
    if any(prefix in lower for prefix in [
        'glyph_', 'ritual_', 'familiar_', 'source_', 'mana_', 'spell_', 'effect_',
        'rune_', 'altar_', 'pedestal_', 'summon_', 'botania_', 'create_', 
        'mechanism_', 'gear_', 'forest_', 'incantation_', 'sigil_', 'kubejs_'
    ]):
        return True
    return False

class TranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.ui_lang = UI_LANG if UI_LANG in UI_TRANSLATIONS else "ru"
        self.title(self.t("app_title"))
        self.geometry("1150x850")
        self.resizable(False, False)
        
        if os.path.exists("icon.ico"):
            try: self.iconbitmap("icon.ico")
            except: pass
        
        self.ai_process = None
        self.mc_dir = os.getcwd()
        self.ai_model_path = ""
        self.is_running = False
        self.is_paused = False
        
        self.start_time = None
        self.total_strings = 0
        self.translated_strings = 0
        self.last_eta_update = 0
        self.auto_scroll = True
        
        self.cache_std, changes_std = load_and_polish_cache(CACHE_FILE_STD)
        self.cache_ai, changes_ai = load_and_polish_cache(CACHE_FILE_AI)
        
        self.active_cache = self.cache_std
        self.active_cache_file = CACHE_FILE_STD

        self.build_ui()
        
        total_changes = changes_std + changes_ai
        if total_changes > 0:
            self.log_colored(self.t("cache_polished", count=total_changes), "magenta")

    def t(self, key, **kwargs):
        current_lang = UI_TRANSLATIONS.get(self.ui_lang, {})
        text = current_lang.get(key)
        if text is None:
            text = UI_TRANSLATIONS.get("en", {}).get(key)
        if text is None:
            text = UI_TRANSLATIONS.get("ru", {}).get(key, key)
        if kwargs:
            return text.format(**kwargs)
        return text

    def log_t(self, key, color_tag="white", **kwargs):
        self.log_colored(self.t(key, **kwargs), color_tag)

    def status_t(self, key, val=None, **kwargs):
        self.set_status(self.t(key, **kwargs), val)

    def save_ui_language(self):
        config.read(settings_file, encoding="utf-8")
        if "GENERAL" not in config:
            config["GENERAL"] = {}
        config["GENERAL"]["ui_language"] = self.ui_lang
        with open(settings_file, "w", encoding="utf-8") as f:
            config.write(f)

    def on_ui_language_change(self, selected_label):
        lang_map = {label: code for code, label in UI_LANGUAGE_LABELS.items()}
        new_lang = lang_map.get(selected_label, "ru")
        if new_lang != self.ui_lang:
            self.ui_lang = new_lang
            self.save_ui_language()
            self.refresh_ui_texts()

    def build_ui(self):
        self.frame_left = ctk.CTkScrollableFrame(self, width=370)
        self.frame_left.pack(side="left", fill="y", padx=10, pady=10)

        self.var_ui_lang = ctk.StringVar(value=UI_LANGUAGE_LABELS.get(self.ui_lang, UI_LANGUAGE_LABELS["ru"]))
        self.lbl_ui_language = ctk.CTkLabel(self.frame_left, text=self.t("ui_language"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_ui_language.pack(pady=(5, 5))
        self.menu_ui_language = ctk.CTkOptionMenu(
            self.frame_left,
            variable=self.var_ui_lang,
            values=list(UI_LANGUAGE_LABELS.values()),
            command=self.on_ui_language_change
        )
        self.menu_ui_language.pack(fill="x", padx=20)
        
        self.lbl_minecraft_folder = ctk.CTkLabel(self.frame_left, text=self.t("minecraft_folder"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_minecraft_folder.pack(pady=(15, 5))
        self.lbl_folder = ctk.CTkLabel(self.frame_left, text=self.mc_dir[-30:], text_color="gray")
        self.lbl_folder.pack()
        self.btn_select_folder = ctk.CTkButton(self.frame_left, text=self.t("select_folder"), command=self.select_folder, fg_color="#444")
        self.btn_select_folder.pack(pady=5, fill="x", padx=20)

        self.lbl_target_language = ctk.CTkLabel(self.frame_left, text=self.t("target_language"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_target_language.pack(pady=(15, 5))
        self.var_lang = ctk.StringVar(value="Русский")
        ctk.CTkOptionMenu(self.frame_left, variable=self.var_lang, values=list(LANGUAGES.keys())).pack(fill="x", padx=20)

        self.lbl_save_method = ctk.CTkLabel(self.frame_left, text=self.t("save_method"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_save_method.pack(pady=(15, 5))
        self.var_output = ctk.StringVar(value="resourcepack")
        self.rb_output_resourcepack = ctk.CTkRadioButton(self.frame_left, text=self.t("output_resourcepack"), variable=self.var_output, value="resourcepack", command=self.update_output_ui)
        self.rb_output_resourcepack.pack(anchor="w", padx=20, pady=5)
        
        self.entry_rp_name = ctk.CTkEntry(self.frame_left, placeholder_text=self.t("rp_name_placeholder"))
        self.entry_rp_name.insert(0, "MineAI_Pack")
        self.entry_rp_name.pack(fill="x", padx=40, pady=(0, 5))

        self.rb_output_inplace = ctk.CTkRadioButton(self.frame_left, text=self.t("output_inplace"), variable=self.var_output, value="inplace", command=self.update_output_ui)
        self.rb_output_inplace.pack(anchor="w", padx=20, pady=5)

        self.lbl_what_translate = ctk.CTkLabel(self.frame_left, text=self.t("what_translate"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_what_translate.pack(pady=(15, 5))
        self.var_mods = ctk.BooleanVar(value=True)
        self.var_books = ctk.BooleanVar(value=True)
        self.var_quests = ctk.BooleanVar(value=True)
        self.cb_mods = ctk.CTkCheckBox(self.frame_left, text=self.t("mods_checkbox"), variable=self.var_mods)
        self.cb_mods.pack(anchor="w", padx=20, pady=2)
        self.cb_books = ctk.CTkCheckBox(self.frame_left, text=self.t("books_checkbox"), variable=self.var_books)
        self.cb_books.pack(anchor="w", padx=20, pady=2)
        self.cb_quests = ctk.CTkCheckBox(self.frame_left, text=self.t("quests_checkbox"), variable=self.var_quests)
        self.cb_quests.pack(anchor="w", padx=20, pady=2)

        self.lbl_engine = ctk.CTkLabel(self.frame_left, text=self.t("engine"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_engine.pack(pady=(15, 5))
        self.var_engine = ctk.StringVar(value="google")
        self.rb_engine_google = ctk.CTkRadioButton(self.frame_left, text=self.t("engine_google"), variable=self.var_engine, value="google", command=self.update_engine_ui)
        self.rb_engine_google.pack(anchor="w", padx=20, pady=5)
        self.rb_engine_deepl = ctk.CTkRadioButton(self.frame_left, text=self.t("engine_deepl"), variable=self.var_engine, value="deepl", command=self.update_engine_ui)
        self.rb_engine_deepl.pack(anchor="w", padx=20, pady=5)
        self.rb_engine_ai = ctk.CTkRadioButton(self.frame_left, text=self.t("engine_ai"), variable=self.var_engine, value="ai", command=self.update_engine_ui)
        self.rb_engine_ai.pack(anchor="w", padx=20, pady=5)

        self.frame_deepl = ctk.CTkFrame(self.frame_left, fg_color="transparent")
        self.entry_deepl_key = ctk.CTkEntry(self.frame_deepl, placeholder_text=self.t("deepl_placeholder"))
        self.entry_deepl_key.pack(fill="x")

        self.frame_ai = ctk.CTkFrame(self.frame_left, fg_color="transparent")
        self.lbl_ai_model = ctk.CTkLabel(self.frame_ai, text=self.t("ai_model_not_selected"), text_color="yellow")
        self.lbl_ai_model.pack()
        self.btn_select_model = ctk.CTkButton(self.frame_ai, text=self.t("select_model"), command=self.select_model, fg_color="#555")
        self.btn_select_model.pack(fill="x", pady=(0, 10))
        
        self.lbl_gpu = ctk.CTkLabel(self.frame_ai, text=f"{self.t('gpu_label', value=99)}{self.t('gpu_max')}", font=ctk.CTkFont(size=12))
        self.lbl_gpu.pack(anchor="w", pady=(5, 0))
        self.slider_gpu = ctk.CTkSlider(self.frame_ai, from_=0, to=99, number_of_steps=99, command=self.update_gpu_label)
        self.slider_gpu.set(99)
        self.slider_gpu.pack(fill="x", pady=(0, 5))
        
        self.var_ai_mode = ctk.StringVar(value="safe")
        self.rb_ai_mode_safe = ctk.CTkRadioButton(self.frame_ai, text=self.t("ai_mode_safe"), variable=self.var_ai_mode, value="safe")
        self.rb_ai_mode_safe.pack(anchor="w", pady=2)
        self.rb_ai_mode_context = ctk.CTkRadioButton(self.frame_ai, text=self.t("ai_mode_context"), variable=self.var_ai_mode, value="context")
        self.rb_ai_mode_context.pack(anchor="w", pady=2)

        self.lbl_process_mode = ctk.CTkLabel(self.frame_left, text=self.t("process_mode"), font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_process_mode.pack(pady=(15, 5))
        self.var_mode = ctk.StringVar(value="append")
        self.rb_mode_append = ctk.CTkRadioButton(self.frame_left, text=self.t("mode_append"), variable=self.var_mode, value="append")
        self.rb_mode_append.pack(anchor="w", padx=20, pady=2)
        self.rb_mode_skip = ctk.CTkRadioButton(self.frame_left, text=self.t("mode_skip"), variable=self.var_mode, value="skip")
        self.rb_mode_skip.pack(anchor="w", padx=20, pady=2)
        self.rb_mode_force = ctk.CTkRadioButton(self.frame_left, text=self.t("mode_force"), variable=self.var_mode, value="force")
        self.rb_mode_force.pack(anchor="w", padx=20, pady=2)

        self.btn_analyze = ctk.CTkButton(self.frame_left, text=self.t("analyze"), fg_color="#0066cc", hover_color="#004c99", command=self.start_analysis)
        self.btn_analyze.pack(pady=(20, 10), fill="x", padx=20)
        
        self.btn_start = ctk.CTkButton(self.frame_left, text=self.t("start"), fg_color="#28a745", hover_color="#218838", height=40, font=ctk.CTkFont(weight="bold"), command=self.start_translation)
        self.btn_start.pack(pady=5, fill="x", padx=20)

        self.btn_pause = ctk.CTkButton(self.frame_left, text=self.t("pause"), fg_color="#ffc107", text_color="black", hover_color="#e0a800", height=40, font=ctk.CTkFont(weight="bold"), command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(pady=5, fill="x", padx=20)

        self.btn_stop = ctk.CTkButton(self.frame_left, text=self.t("stop"), fg_color="#dc3545", hover_color="#c82333", height=40, font=ctk.CTkFont(weight="bold"), command=self.stop_process, state="disabled")
        self.btn_stop.pack(pady=(5, 10), fill="x", padx=20)

        self.frame_right = ctk.CTkFrame(self)
        self.frame_right.pack(side="right", fill="both", expand=True, padx=(0, 10), pady=10)
        
        self.textbox = ctk.CTkTextbox(self.frame_right, state="disabled", font=ctk.CTkFont(family="Consolas", size=14, weight="bold"))
        self.textbox.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.textbox.tag_config("green", foreground="#2ecc71")
        self.textbox.tag_config("yellow", foreground="#f1c40f")
        self.textbox.tag_config("red", foreground="#e74c3c")
        self.textbox.tag_config("cyan", foreground="#00e5ff")
        self.textbox.tag_config("magenta", foreground="#b000ff")
        self.textbox.tag_config("dim", foreground="#888888")
        self.textbox.tag_config("white", foreground="#ffffff")
        
        self.textbox.bind("<Button-1>", self.on_user_interaction)
        self.textbox.bind("<Key>", self.on_user_interaction)
        self.textbox.bind("<MouseWheel>", self.on_user_interaction)
        
        self.progress_bar = ctk.CTkProgressBar(self.frame_right)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 5))
        self.progress_bar.set(0)
        
        self.lbl_status = ctk.CTkLabel(self.frame_right, text=self.t("status_waiting"), font=ctk.CTkFont(size=14))
        self.lbl_status.pack(pady=(0, 10))

        self.update_engine_ui()
        self.update_output_ui()

    def refresh_ui_texts(self):
        self.title(self.t("app_title"))
        self.lbl_ui_language.configure(text=self.t("ui_language"))
        self.lbl_minecraft_folder.configure(text=self.t("minecraft_folder"))
        self.btn_select_folder.configure(text=self.t("select_folder"))
        self.lbl_target_language.configure(text=self.t("target_language"))
        self.lbl_save_method.configure(text=self.t("save_method"))
        self.rb_output_resourcepack.configure(text=self.t("output_resourcepack"))
        self.rb_output_inplace.configure(text=self.t("output_inplace"))
        self.entry_rp_name.configure(placeholder_text=self.t("rp_name_placeholder"))
        self.lbl_what_translate.configure(text=self.t("what_translate"))
        self.cb_mods.configure(text=self.t("mods_checkbox"))
        self.cb_books.configure(text=self.t("books_checkbox"))
        self.cb_quests.configure(text=self.t("quests_checkbox"))
        self.lbl_engine.configure(text=self.t("engine"))
        self.rb_engine_google.configure(text=self.t("engine_google"))
        self.rb_engine_deepl.configure(text=self.t("engine_deepl"))
        self.rb_engine_ai.configure(text=self.t("engine_ai"))
        self.entry_deepl_key.configure(placeholder_text=self.t("deepl_placeholder"))
        if not self.ai_model_path:
            self.lbl_ai_model.configure(text=self.t("ai_model_not_selected"))
        self.btn_select_model.configure(text=self.t("select_model"))
        self.update_gpu_label(self.slider_gpu.get())
        self.rb_ai_mode_safe.configure(text=self.t("ai_mode_safe"))
        self.rb_ai_mode_context.configure(text=self.t("ai_mode_context"))
        self.lbl_process_mode.configure(text=self.t("process_mode"))
        self.rb_mode_append.configure(text=self.t("mode_append"))
        self.rb_mode_skip.configure(text=self.t("mode_skip"))
        self.rb_mode_force.configure(text=self.t("mode_force"))
        self.btn_analyze.configure(text=self.t("analyze"))
        self.btn_start.configure(text=self.t("start"))
        self.btn_pause.configure(text=self.t("resume") if self.is_paused else self.t("pause"))
        self.btn_stop.configure(text=self.t("stop"))
        self.lbl_status.configure(text=self.t("status_waiting"))

    def wait_if_paused(self):
        while self.is_paused and self.is_running:
            time.sleep(0.5)

    def toggle_pause(self):
        if self.is_paused:
            self.is_paused = False
            self.btn_pause.configure(text=self.t("pause"), fg_color="#ffc107", text_color="black")
            self.log_colored(self.t("log_resumed"), "green")
        else:
            self.is_paused = True
            self.btn_pause.configure(text=self.t("resume"), fg_color="#17a2b8", text_color="white")
            self.log_colored(self.t("log_paused"), "yellow")

    def update_gpu_label(self, value):
        val = int(value)
        text = self.t("gpu_label", value=val)
        if val == 0:
            text += self.t("gpu_cpu")
        elif val == 99:
            text += self.t("gpu_max")
        self.lbl_gpu.configure(text=text)

    def update_output_ui(self):
        if self.var_output.get() == "resourcepack":
            self.entry_rp_name.configure(state="normal")
        else:
            self.entry_rp_name.configure(state="disabled")

    def update_engine_ui(self):
        engine = self.var_engine.get()
        self.frame_deepl.pack_forget()
        self.frame_ai.pack_forget()
        if engine == "deepl": self.frame_deepl.pack(fill="x", padx=20, pady=5)
        elif engine == "ai": self.frame_ai.pack(fill="x", padx=20, pady=5)

    def on_user_interaction(self, event=None):
        self.auto_scroll = (self.textbox.yview()[1] >= 0.99)

    def select_folder(self):
        folder = filedialog.askdirectory(title=self.t("select_folder_dialog"))
        if folder:
            self.mc_dir = folder
            self.lbl_folder.configure(text=f"...{folder[-25:]}" if len(folder) > 25 else folder)

    def select_model(self):
        file = filedialog.askopenfilename(title=self.t("select_model_dialog"), filetypes=[("GGUF Models", "*.gguf")])
        if file:
            self.ai_model_path = file
            self.lbl_ai_model.configure(text=os.path.basename(file), text_color="green")

    def log_colored(self, message, color_tag="white"):
        self.textbox.configure(state="normal")
        at_bottom = self.textbox.yview()[1] >= 0.99
        self.textbox.insert("end", message + "\n", color_tag)
        if self.auto_scroll or at_bottom:
            self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def log_table_row(self, icon, name, m_type, trans_c, en_c, pct):
        color = "green" if pct >= 90 else ("yellow" if pct >= 50 else "red")
        name_str = f"{icon} {name[:34]:<35}"
        type_str = f"[{m_type}]".ljust(15)
        count_str = f"{trans_c}/{en_c}".ljust(12)
        pct_str = f"{pct}%"

        self.textbox.configure(state="normal")
        at_bottom = self.textbox.yview()[1] >= 0.99
        self.textbox.insert("end", name_str, "cyan")
        self.textbox.insert("end", type_str, "magenta")
        self.textbox.insert("end", count_str, "white")
        self.textbox.insert("end", pct_str + "\n", color)
        if self.auto_scroll or at_bottom:
            self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def set_status(self, text, val=None):
        if val is not None:
            self.progress_bar.set(val)
        self.lbl_status.configure(text=text)

    def update_eta(self):
        if not self.start_time or self.translated_strings == 0:
            return self.t("eta_calculating")
        elapsed = time.time() - self.start_time
        if elapsed < 5:
            return self.t("eta_calculating")
        speed = self.translated_strings / elapsed
        remaining = self.total_strings - self.translated_strings
        if remaining <= 0:
            return self.t("eta_done")
        eta_seconds = remaining / speed
        if eta_seconds < 60:
            return self.t("eta_seconds", seconds=int(eta_seconds))
        elif eta_seconds < 3600:
            return self.t("eta_minutes", minutes=int(eta_seconds//60), seconds=int(eta_seconds%60))
        else:
            return self.t("eta_hours", hours=int(eta_seconds//3600), minutes=int((eta_seconds%3600)//60))

    def lock_ui(self, lock=True):
        self.btn_analyze.configure(state="disabled" if lock else "normal")
        self.btn_start.configure(state="disabled" if lock else "normal")
        self.btn_stop.configure(state="normal" if lock else "disabled")
        self.btn_pause.configure(state="normal" if lock else "disabled")

    def stop_process(self):
        self.is_running = False
        self.is_paused = False
        self.set_status(self.t("status_stopping"), 1.0)
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled")
        if self.ai_process:
            try:
                self.ai_process.terminate()
            except: pass

    def start_analysis(self):
        self.lock_ui(True)
        self.is_running = True
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        threading.Thread(target=self.run_analysis, daemon=True).start()

    def run_analysis(self):
        lang_settings = LANGUAGES[self.var_lang.get()]
        target_file = f"{lang_settings['file']}.json"
        l_regex = lang_settings.get('regex', r'[А-Яа-яЁё]')
        
        mods_dir = os.path.join(self.mc_dir, "mods")
        quests_dir = os.path.join(self.mc_dir, "config", "ftbquests", "quests")

        self.log_t("scan_pack", "yellow", language=lang_settings['name'])
        header = f"{self.t('analysis_header_file'):<37}{self.t('analysis_header_type'):<15}{self.t('analysis_header_lines'):<12}{self.t('analysis_header_progress')}"
        self.log_colored(header, "white")
        self.log_colored("-" * 75, "dim")
        
        total_en, total_trans = 0, 0
        
        jar_files = []
        if os.path.exists(mods_dir) and (self.var_mods.get() or self.var_books.get()):
            jar_files = [os.path.join(mods_dir, f) for f in os.listdir(mods_dir) if f.endswith('.jar')]

        for i, filepath in enumerate(jar_files):
            if not self.is_running: break
            self.wait_if_paused()
            mod_name = get_mod_name(filepath)
            self.status_t("analyzing_mod", i / (len(jar_files) + 1), name=mod_name)
            try:
                with zipfile.ZipFile(filepath, 'r') as zin:
                    trans_files = {item.filename.lower(): item for item in zin.infolist() if target_file in item.filename.lower() or f"/{lang_settings['file']}/" in item.filename.lower()}
                    
                    if self.var_mods.get():
                        int_en_c, int_trans_c = 0, 0
                        for item in zin.infolist():
                            if item.filename.lower().endswith('en_us.json') and not any(x in item.filename.lower() for x in ('patchouli', 'lexicon', 'guide')):
                                try:
                                    en_data = load_lenient_json(zin.read(item))
                                    trans_t = item.filename.lower().replace('en_us.json', target_file)
                                    trans_data = load_lenient_json(zin.read(trans_files[trans_t])) if trans_t in trans_files else {}
                                    int_en_c += len([k for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v) and not is_technical_term(v)])
                                    int_trans_c += sum(1 for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v) and not is_technical_term(v) and (str(trans_data.get(k,"")) != v and str(trans_data.get(k,"")).strip() != ""))
                                except: pass
                        if int_en_c > 0:
                            total_en += int_en_c; total_trans += int_trans_c
                            self.log_table_row("📦", mod_name, self.t("type_interface"), int_trans_c, int_en_c, int(int_trans_c/int_en_c*100))

                    if self.var_books.get():
                        book_en_c, book_trans_c = 0, 0
                        md_en_c, md_trans_c = 0, 0
                        for item in zin.infolist():
                            f_lower = item.filename.lower()
                            is_json_book = f_lower.endswith('.json') and ('/en_us/' in f_lower) and any(x in f_lower for x in ('patchouli', 'lexicon', 'guide'))
                            is_md_book = (f_lower.endswith('.md') or f_lower.endswith('.txt')) and any(x in f_lower for x in ('/en_us/', '/ae2guide/', '/guide/', '/manual/', '/lexicon/'))
                            
                            if is_json_book:
                                try:
                                    en_data = load_lenient_json(zin.read(item))
                                    trans_t = f_lower.replace('/en_us/', f"/{lang_settings['file']}/")
                                    trans_data = load_lenient_json(zin.read(trans_files[trans_t])) if trans_t in trans_files else {}
                                    en_strings = [s for s in extract_book_strings(en_data) if s.strip() and re.search(r'[a-zA-Z]', s)]
                                    trans_strings = [s for s in extract_book_strings(trans_data) if s.strip()] if trans_data else []
                                    en_c = len(en_strings)
                                    trans_c = sum(1 for idx, s in enumerate(en_strings) if idx < len(trans_strings) and trans_strings[idx] != s)
                                except: pass
                            elif is_md_book:
                                try:
                                    en_text = zin.read(item).decode('utf-8-sig', errors='ignore')
                                    if '/en_us/' in f_lower:
                                        trans_t = f_lower.replace('/en_us/', f"/{lang_settings['file']}/")
                                    else:
                                        trans_t = f_lower
                                        
                                    trans_text = zin.read(trans_files[trans_t]).decode('utf-8-sig', errors='ignore') if trans_t in trans_files else ""
                                    en_lines = en_text.split('\n')
                                    trans_lines = trans_text.split('\n') if trans_text else []
                                    
                                    in_yaml = False
                                    for idx, s in enumerate(en_lines):
                                        s_stripped = s.strip()
                                        if s_stripped == '---':
                                            in_yaml = not in_yaml
                                            continue
                                            
                                        if in_yaml:
                                            if s_stripped.lower().startswith('title:'):
                                                match = re.match(r'^(\s*title\s*:\s*[\'"]?)(.*?)([\'"]?)$', s, re.IGNORECASE)
                                                if match and re.search(r'[a-zA-Z]', match.group(2)):
                                                    md_en_c += 1
                                                    if idx < len(trans_lines) and re.search(l_regex, trans_lines[idx]): md_trans_c += 1
                                            continue
                                            
                                        if s_stripped.startswith('<') or s_stripped.startswith('!['):
                                            continue
                                            
                                        if s.strip() and re.search(r'[a-zA-Z]', s) and not is_technical_term(s):
                                            md_en_c += 1
                                            if idx < len(trans_lines) and re.search(l_regex, trans_lines[idx]):
                                                md_trans_c += 1
                                except: pass
                                
                        if book_en_c > 0:
                            total_en += book_en_c; total_trans += book_trans_c
                            self.log_table_row("📖", mod_name, self.t("type_book_json"), book_trans_c, book_en_c, int(book_trans_c/book_en_c*100))
                        if md_en_c > 0:
                            total_en += md_en_c; total_trans += md_trans_c
                            self.log_table_row("📝", mod_name, self.t("type_book_md"), md_trans_c, md_en_c, int(md_trans_c/md_en_c*100))

            except: pass

        snbt_files = []
        if os.path.exists(quests_dir) and self.var_quests.get():
            for root, _, files in os.walk(quests_dir):
                snbt_files.extend([os.path.join(root, f) for f in files if f.endswith('.snbt')])
                
        for i, filepath in enumerate(snbt_files):
            if not self.is_running: break
            self.wait_if_paused()
            self.status_t("analyzing_quest", (len(jar_files) + i) / (len(jar_files) + len(snbt_files)), name=os.path.basename(filepath))
            try:
                with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
                strings = re.findall(r'(?:"|)(?:title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.IGNORECASE)
                desc_blocks = re.findall(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', content, re.DOTALL | re.IGNORECASE)
                for b in desc_blocks: strings.extend(re.findall(r'"((?:[^"\\]|\\.)*)"', b))
                valid_str = list(set([s for s in strings if s.strip() and not is_translation_key(s) and re.search(r'[a-zA-Z]', s)]))
                en_c = len(valid_str)
                trans_c = sum(1 for s in valid_str if re.search(l_regex, s))
                if en_c > 0:
                    total_en += en_c; total_trans += trans_c
                    self.log_table_row("📜", os.path.basename(filepath), self.t("type_quests"), trans_c, en_c, int(trans_c/en_c*100))
            except: pass

        self.log_colored("-" * 75, "dim")
        if not self.is_running:
            self.log_t("analysis_interrupted", "red")
        elif total_en > 0:
            pct = int((total_trans / total_en) * 100)
            c_color = "green" if pct >= 90 else ("yellow" if pct >= 50 else "red")
            self.log_t("analysis_finished", c_color)
            self.log_t("analysis_summary", c_color, pct=pct, total=total_en)
        else:
            self.log_t("no_files_to_translate", "red")
            
        self.status_t("done", 1.0)
        self.lock_ui(False)

    def start_translation(self):
        self.lock_ui(True)
        self.is_running = True
        self.is_paused = False
        self.btn_pause.configure(text=self.t("pause"), fg_color="#ffc107", text_color="black")
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        threading.Thread(target=self._run_translation_wrapper, daemon=True).start()

    def _run_translation_wrapper(self):
        try:
            self.run_translation()
        except Exception as e:
            error_text = traceback.format_exc()
            self.log_t("critical_error", "red")
            self.log_colored(error_text, "red")
            self.status_t("check_logs_error")
            self.lock_ui(False)

    def estimate_total_strings(self, jar_files, snbt_files, lang_settings, mode_overwrite):
        total = 0
        target_file = f"{lang_settings['file']}.json"
        l_regex = lang_settings.get('regex', r'[А-Яа-яЁё]')

        for filepath in jar_files:
            if not self.is_running: return total
            self.wait_if_paused()
            try:
                with zipfile.ZipFile(filepath, 'r') as zin:
                    trans_files = {item.filename.lower(): item for item in zin.infolist() 
                                   if target_file in item.filename.lower() or f"/{lang_settings['file']}/" in item.filename.lower()}
                    for item in zin.infolist():
                        f_lower = item.filename.lower()
                        is_json_book = f_lower.endswith('.json') and ('/en_us/' in f_lower) and any(x in f_lower for x in ('patchouli', 'lexicon', 'guide'))
                        is_md_book = (f_lower.endswith('.md') or f_lower.endswith('.txt')) and any(x in f_lower for x in ('/en_us/', '/ae2guide/', '/guide/', '/manual/', '/lexicon/'))
                        is_lang = (f_lower.endswith('en_us.json') and not is_json_book)

                        if self.var_mods.get() and is_lang:
                            try: en_data = load_lenient_json(zin.read(item))
                            except: continue
                            try: trans_data = load_lenient_json(zin.read(trans_files.get(f_lower.replace('en_us.json', target_file), None))) if f_lower.replace('en_us.json', target_file) in trans_files else {}
                            except: trans_data = {}
                            
                            for k, v in en_data.items():
                                if isinstance(v, str) and re.search(r'[a-zA-Z]', v) and not is_technical_term(v):
                                    if mode_overwrite == "force" or not (k in trans_data and isinstance(trans_data[k], str) and trans_data[k].strip()):
                                        total += 1

                        elif self.var_books.get() and is_json_book:
                            try: en_data = load_lenient_json(zin.read(item))
                            except: continue
                            en_strings = [s for s in extract_book_strings(en_data) if s.strip() and re.search(r'[a-zA-Z]', s) and not is_technical_term(s)]
                            total += len(en_strings)
                            
                        elif self.var_books.get() and is_md_book:
                            try: en_text = zin.read(item).decode('utf-8-sig', errors='ignore')
                            except: continue
                            en_lines = en_text.split('\n')
                            in_yaml = False
                            for en_s in en_lines:
                                s_stripped = en_s.strip()
                                if s_stripped == '---':
                                    in_yaml = not in_yaml
                                    continue
                                    
                                if in_yaml:
                                    if s_stripped.lower().startswith('title:'):
                                        match = re.match(r'^(\s*title\s*:\s*[\'"]?)(.*?)([\'"]?)$', en_s, re.IGNORECASE)
                                        if match and re.search(r'[a-zA-Z]', match.group(2)):
                                            total += 1
                                    continue
                                    
                                if s_stripped.startswith('<') or s_stripped.startswith('!['):
                                    continue
                                    
                                if en_s.strip() and re.search(r'[a-zA-Z]', en_s) and not is_technical_term(en_s):
                                    total += 1
            except: pass

        for filepath in snbt_files:
            if not self.is_running: return total
            self.wait_if_paused()
            try:
                with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
                strings = re.findall(r'(?:"|)(?:title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.IGNORECASE)
                desc_blocks = re.findall(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', content, re.DOTALL | re.IGNORECASE)
                for b in desc_blocks: strings.extend(re.findall(r'"((?:[^"\\]|\\.)*)"', b))
                valid = [s for s in strings if s.strip() and not is_translation_key(s) and re.search(r'[a-zA-Z]', s)]
                if mode_overwrite == "force":
                    total += len(valid)
                else:
                    total += sum(1 for s in valid if not re.search(l_regex, s))
            except: pass
        return total

    def run_translation(self):
        engine = self.var_engine.get()
        if engine == "ai":
            self.active_cache = self.cache_ai
            self.active_cache_file = CACHE_FILE_AI
        else:
            self.active_cache = self.cache_std
            self.active_cache_file = CACHE_FILE_STD

        lang_settings = LANGUAGES[self.var_lang.get()]
        mode_overwrite = self.var_mode.get()
        output_mode = self.var_output.get()
        
        mods_dir = os.path.join(self.mc_dir, "mods")
        quests_dir = os.path.join(self.mc_dir, "config", "ftbquests", "quests")
        rp_dir = os.path.join(self.mc_dir, "resourcepacks")

        if engine == "deepl" and not self.entry_deepl_key.get().strip():
            self.log_t("deepl_missing_key", "red")
            self.lock_ui(False); return
        if engine == "ai" and not self.ai_model_path:
            self.log_t("ai_model_missing", "red")
            self.lock_ui(False); return

        jar_files = []
        if os.path.exists(mods_dir) and (self.var_mods.get() or self.var_books.get()):
            jar_files = [os.path.join(mods_dir, f) for f in os.listdir(mods_dir) if f.endswith('.jar')]
        snbt_files = []
        if self.var_quests.get() and os.path.exists(quests_dir):
            for root, _, files in os.walk(quests_dir):
                snbt_files.extend([os.path.join(root, f) for f in files if f.endswith('.snbt')])

        total_files = len(jar_files) + len(snbt_files)
        if total_files == 0:
            self.log_t("nothing_to_translate", "red")
            self.lock_ui(False); return

        self.log_t("counting_strings", "yellow")
        self.total_strings = self.estimate_total_strings(jar_files, snbt_files, lang_settings, mode_overwrite)
        self.log_t("strings_found", "cyan", count=self.total_strings)

        if engine == "ai" and not self.setup_and_start_ai():
            self.lock_ui(False); return

        rp_zip_path = None
        rp_zip_handle = None
        written_files = set()
        
        if output_mode == "resourcepack":
            if not os.path.exists(rp_dir): os.makedirs(rp_dir)
            
            base_rp_name = self.entry_rp_name.get().strip()
            base_rp_name = re.sub(r'[\\/*?:"<>|]', "", base_rp_name)
            if not base_rp_name: base_rp_name = f"MineAI_{lang_settings['name']}_Pack"
            if not base_rp_name.lower().endswith(".zip"): base_rp_name += ".zip"
                
            rp_name = base_rp_name
            counter = 1
            while True:
                rp_zip_path = os.path.join(rp_dir, rp_name)
                if os.path.exists(rp_zip_path):
                    try: 
                        os.remove(rp_zip_path)
                        self.log_t("old_archive_removed", "yellow", name=rp_name)
                        break
                    except Exception:
                        self.log_t("archive_in_use", "yellow", name=rp_name)
                        rp_name = base_rp_name.replace(".zip", f"_{counter}.zip")
                        counter += 1
                else:
                    break
            
            with zipfile.ZipFile(rp_zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                mcmeta = {"pack": {"pack_format": 15, "description": f"{rp_name.replace('.zip', '')} - Translated by MineAI"}}
                zout.writestr("pack.mcmeta", json.dumps(mcmeta, indent=2))
                written_files.add("pack.mcmeta")
            
            rp_zip_handle = zipfile.ZipFile(rp_zip_path, 'a', compression=zipfile.ZIP_DEFLATED)
            self.log_t("resourcepack_created", "cyan", path=rp_zip_path)

        self.log_t("translation_started", "yellow", language=lang_settings['name'], cache=self.active_cache_file)
        
        self.start_time = time.time()
        self.translated_strings = 0
        self.last_eta_update = time.time()
        self.auto_scroll = True

        processed = 0
        
        try:
            for filepath in jar_files:
                if not self.is_running: break
                self.wait_if_paused()
                self.process_jar(filepath, engine, mode_overwrite, output_mode, lang_settings, rp_zip_path, rp_zip_handle, written_files)
                processed += 1
                self.status_t("processed_mods", processed / total_files, processed=processed, total=len(jar_files), eta=self.update_eta())
                
            for filepath in snbt_files:
                if not self.is_running: break
                self.wait_if_paused()
                self.process_snbt(filepath, engine, mode_overwrite, lang_settings)
                processed += 1
                self.status_t("processed_quests", processed / total_files, processed=processed, total=total_files, eta=self.update_eta())

            save_cache_data(self.active_cache, self.active_cache_file)
        finally:
            if rp_zip_handle:
                rp_zip_handle.close()

        if not self.is_running:
            self.log_t("process_stopped", "red")
        else:
            self.log_t("translation_finished", "green")
            if output_mode == "resourcepack":
                self.log_t("enable_resourcepack", "yellow")
                if len(snbt_files) > 0:
                    self.log_t("quests_saved_config", "dim")
        
        self.status_t("all_tasks_done" if self.is_running else "stopped", 1.0)
        if self.ai_process: 
            try: self.ai_process.terminate() 
            except: pass
        self.lock_ui(False)

    def setup_and_start_ai(self):
        try:
            if requests.get(KOBOLD_API.replace("chat/completions", "models"), timeout=1).status_code == 200:
                self.log_t("ai_already_running", "green")
                return True
        except: pass

        self.log_t("ai_starting", "cyan", model=os.path.basename(self.ai_model_path))
        kobold_exe = os.path.join(AI_DIR, "koboldcpp.exe") if os.path.exists(os.path.join(AI_DIR, "koboldcpp.exe")) else "koboldcpp"
        
        gpu_layers = str(int(self.slider_gpu.get()))
        
        try:
            self.ai_process = subprocess.Popen([
                kobold_exe, self.ai_model_path, 
                "--port", "5001", 
                "--quiet", 
                "--contextsize", "8192",
                "--usecublas",       
                "--gpulayers", gpu_layers  
            ], stdout=subprocess.DEVNULL)
        except Exception as e:
            self.log_t("ai_start_error", "red", error=e)
            return False
            
        for i in range(180):
            if not self.is_running: return False
            self.status_t("ai_warming", None, current=i, total=180)
            try:
                if requests.get(KOBOLD_API.replace("chat/completions", "models"), timeout=1).status_code == 200:
                    self.log_t("ai_started", "green")
                    return True
            except: time.sleep(1)
        self.log_t("ai_timeout", "red")
        return False

    def translate_engine(self, data_dict, engine, lang_settings, context_name=""):
        keys = list(data_dict.keys())
        result = {}
        to_translate = {}
        in_cache_count = 0
        
        for k in keys:
            if not self.is_running: break
            self.wait_if_paused()
            
            text = data_dict[k]
            cache_key = f"{lang_settings['api']}_{text}"
            
            if cache_key in self.active_cache:
                result[k] = self.active_cache[cache_key]
                in_cache_count += 1
                self.translated_strings += 1
                if time.time() - self.last_eta_update > 0.5: 
                    self.status_t("reading_cache", None, translated=self.translated_strings, total=self.total_strings, eta=self.update_eta())
                    self.last_eta_update = time.time()
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
                self.translated_strings += 1
                continue
                
            to_translate[k] = {"original": text, "masked": masked, "mapping": mapping}

        if in_cache_count > 0:
            self.log_t("cache_hits", "dim", count=in_cache_count)

        if not to_translate or not self.is_running: 
            return result

        if engine == "google":
            chunks = []
            curr_keys, curr_text = [], ""
            max_chunk_chars = 4500
            max_chunk_items = 40
            for k, val in to_translate.items():
                if len(curr_text) + len(val["masked"]) > max_chunk_chars or len(curr_keys) >= max_chunk_items:
                    chunks.append((curr_keys, curr_text))
                    curr_keys, curr_text = [k], val["masked"]
                else:
                    curr_keys.append(k)
                    curr_text = curr_text + " |~| " + val["masked"] if curr_text else val["masked"]
            if curr_keys: chunks.append((curr_keys, curr_text))

            def restore_markers(translated_text, mapping):
                for m_idx, (m, orig) in enumerate(mapping.items()):
                    translated_text = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, translated_text)
                return translated_text

            def parse_google_response(response_json):
                return "".join([p[0] for p in response_json[0] if p[0]])

            google_workers = min(len(chunks), max(8, GOOGLE_WORKERS)) if chunks else 1

            with requests.Session() as google_session:
                google_session.headers.update({"User-Agent": "Mozilla/5.0"})

                def translate_chunk(chunk_keys, text_to_send):
                    backoff = 0.5
                    for attempt in range(4):
                        if not self.is_running:
                            return chunk_keys, None
                        try:
                            res = google_session.get(
                                "https://translate.googleapis.com/translate_a/single",
                                params={"client": "gtx", "sl": "en", "tl": lang_settings['api'], "dt": "t", "q": text_to_send},
                                timeout=10,
                            )
                            if res.status_code == 429:
                                self.status_t("google_rate_limit")
                                if attempt < 3:
                                    time.sleep(backoff)
                                    backoff = min(backoff * 2, 4.0)
                                continue
                            res.raise_for_status()
                            parts = re.split(r'\s*\|\s*~\s*\|\s*', parse_google_response(res.json()))
                            if len(parts) == len(chunk_keys):
                                return chunk_keys, parts
                            if len(chunk_keys) == 1 and parts:
                                return chunk_keys, parts
                        except Exception:
                            if attempt < 3:
                                time.sleep(backoff)
                                backoff = min(backoff * 2, 4.0)
                    return chunk_keys, None

                with ThreadPoolExecutor(max_workers=google_workers) as executor:
                    futures = [executor.submit(translate_chunk, ck, txt) for ck, txt in chunks]
                    for future in as_completed(futures):
                        if not self.is_running:
                            break
                        self.wait_if_paused()
                        c_keys, c_parts = future.result()
                        if c_parts:
                            for idx, k in enumerate(c_keys):
                                trans = c_parts[idx].strip()
                                trans = restore_markers(trans, to_translate[k]["mapping"])
                                trans = polish_translation(trans)
                                result[k] = trans
                                self.active_cache[f"{lang_settings['api']}_{to_translate[k]['original']}"] = trans

                                self.translated_strings += 1
                                if time.time() - self.last_eta_update > 2:
                                    self.status_t("translating_strings", None, translated=self.translated_strings, total=self.total_strings, eta=self.update_eta())
                                    self.last_eta_update = time.time()
                                self.log_t("translation_preview", "dim", original=to_translate[k]['original'][:40], translated=trans[:40])
                        else:
                            for k in c_keys:
                                if not self.is_running:
                                    break
                                try:
                                    res = google_session.get(
                                        "https://translate.googleapis.com/translate_a/single",
                                        params={"client": "gtx", "sl": "en", "tl": lang_settings['api'], "dt": "t", "q": to_translate[k]["masked"]},
                                        timeout=10,
                                    )
                                    res.raise_for_status()
                                    trans = parse_google_response(res.json())
                                    trans = restore_markers(trans, to_translate[k]["mapping"])
                                    trans = polish_translation(trans)
                                    result[k] = trans
                                    self.active_cache[f"{lang_settings['api']}_{to_translate[k]['original']}"] = trans

                                    self.translated_strings += 1
                                    if time.time() - self.last_eta_update > 2:
                                        self.status_t("translating_strings", None, translated=self.translated_strings, total=self.total_strings, eta=self.update_eta())
                                        self.last_eta_update = time.time()
                                    self.log_t("translation_preview", "dim", original=to_translate[k]['original'][:40], translated=trans[:40])
                                except Exception:
                                    result[k] = to_translate[k]["original"]
                                    self.translated_strings += 1
                            
        elif engine == "deepl":
            api_key = self.entry_deepl_key.get().strip()
            url = "https://api.deepl.com/v2/translate" if not api_key.endswith(":fx") else "https://api-free.deepl.com/v2/translate"
            b_keys = list(to_translate.keys())
            for i in range(0, len(b_keys), 40):
                if not self.is_running: break
                self.wait_if_paused()
                chunk_keys = b_keys[i:i+40]
                texts = [to_translate[k]["masked"] for k in chunk_keys]
                try:
                    res = requests.post(url, headers={"Authorization": f"DeepL-Auth-Key {api_key}"}, json={"text": texts, "target_lang": lang_settings['deepl']}).json()
                    for idx, k in enumerate(chunk_keys):
                        trans = res["translations"][idx]["text"]
                        for m_idx, (m, orig) in enumerate(to_translate[k]["mapping"].items()):
                            trans = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, trans)
                        
                        trans = polish_translation(trans)
                        result[k] = trans
                        self.active_cache[f"{lang_settings['api']}_{to_translate[k]['original']}"] = trans
                        
                        self.translated_strings += 1
                        if time.time() - self.last_eta_update > 2:
                            self.status_t("translating_strings", None, translated=self.translated_strings, total=self.total_strings, eta=self.update_eta())
                            self.last_eta_update = time.time()
                        self.log_t("translation_preview", "dim", original=to_translate[k]['original'][:40], translated=trans[:40])
                except Exception as e:
                    self.log_t("deepl_error", "red", error=e)
                    for k in chunk_keys: result[k] = to_translate[k]["original"]
                time.sleep(0.5)

        else:  # AI
            ai_mode = self.var_ai_mode.get() if hasattr(self, 'var_ai_mode') else "safe"
            base_batch_size = 40 if ai_mode == "context" else 20
            max_tok = 4096 if ai_mode == "context" else 2048
            
            batch_keys = list(to_translate.keys())

            def process_ai_chunk(chunk_keys):
                if not self.is_running: return False
                sub_dict = {k: to_translate[k]["masked"] for k in chunk_keys}
                
                if ai_mode == "context" and context_name:
                    prompt = f"Ты локализатор. Переведи текст мода/квеста '{context_name}' на {lang_settings['name']}. Адаптируй лор мода. ПРАВИЛА: Не переводи ключи. Сохраняй [#0#]. Верни ТОЛЬКО валидный JSON. Текст: {json.dumps(sub_dict, ensure_ascii=False)}"
                else:
                    prompt = f"Translate the following JSON string values from English to {lang_settings['name']}. RULES: Do not translate keys. Preserve [#0#] tags exactly. Return ONLY valid JSON. Text: {json.dumps(sub_dict, ensure_ascii=False)}"
                
                self.status_t("ai_translating_batch", None, count=len(chunk_keys), eta=self.update_eta())
                try:
                    res = requests.post(KOBOLD_API, json={"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": max_tok}, timeout=300)
                    res.raise_for_status()
                    data = res.json()
                    
                    trans_text = re.sub(r'^```json\s*|^```\s*|```$', '', data['choices'][0]['message']['content'].strip(), flags=re.IGNORECASE).strip()
                    trans_dict = json.loads(trans_text, strict=False)
                    
                    for k in chunk_keys:
                        if k in trans_dict:
                            trans = trans_dict[k]
                            for m_idx, (m, orig) in enumerate(to_translate[k]["mapping"].items()):
                                trans = re.sub(rf'\[\s*#\s*{m_idx}\s*#\s*\]', lambda x, o=orig: o, trans)
                            
                            trans = polish_translation(trans)
                            result[k] = trans
                            self.active_cache[f"{lang_settings['api']}_{to_translate[k]['original']}"] = trans
                            
                            self.translated_strings += 1
                            self.log_t("translation_preview", "dim", original=to_translate[k]['original'][:40], translated=trans[:40])
                        else: 
                            result[k] = to_translate[k]["original"]
                            self.translated_strings += 1
                            self.log_t("ai_skipped_phrase", "yellow", phrase=to_translate[k]['original'][:30])
                    return True
                except requests.exceptions.RequestException:
                    return False 
                except Exception:
                    return False 

            i = 0
            while i < len(batch_keys):
                if not self.is_running: break
                self.wait_if_paused()
                b_keys = batch_keys[i:i+base_batch_size]
                
                success = process_ai_chunk(b_keys)
                
                if not success and self.is_running:
                    self.log_t("ai_chunk_split", "yellow")
                    for j in range(0, len(b_keys), 10):
                        if not self.is_running: break
                        self.wait_if_paused()
                        sub_keys = b_keys[j:j+10]
                        sub_success = process_ai_chunk(sub_keys)
                        if not sub_success and self.is_running:
                            self.log_t("ai_subchunk_failed", "red", count=len(sub_keys))
                            for k in sub_keys:
                                result[k] = to_translate[k]["original"]
                                self.translated_strings += 1
                
                if time.time() - self.last_eta_update > 2:
                    self.status_t("translating_strings", None, translated=self.translated_strings, total=self.total_strings, eta=self.update_eta())
                    self.last_eta_update = time.time()
                    
                i += base_batch_size

        if len(self.active_cache) % 500 == 0: save_cache_data(self.active_cache, self.active_cache_file)
        return result

    def process_jar(self, filepath, engine, mode_overwrite, output_mode, lang_settings, rp_zip_path, rp_zip_handle=None, written_files=None):
        if written_files is None: written_files = set()
        if not self.var_mods.get() and not self.var_books.get(): return
        mod_name = get_mod_name(filepath)
        target_file = f"{lang_settings['file']}.json"
        temp_filepath = filepath + ".temp"
        translated_any = False
        
        try:
            with zipfile.ZipFile(filepath, 'r') as zin:
                zout = zipfile.ZipFile(temp_filepath, 'w', compression=zipfile.ZIP_DEFLATED) if output_mode == "inplace" else None
                try:
                    ru_files_written = set()
                    trans_files = {item.filename.lower(): item for item in zin.infolist() if target_file in item.filename.lower() or f"/{lang_settings['file']}/" in item.filename.lower()}

                    for item in zin.infolist():
                        if not self.is_running: break
                        self.wait_if_paused()
                        f_lower = item.filename.lower()
                        is_json_book = f_lower.endswith('.json') and ('/en_us/' in f_lower) and any(x in f_lower for x in ('patchouli', 'lexicon', 'guide'))
                        is_md_book = (f_lower.endswith('.md') or f_lower.endswith('.txt')) and any(x in f_lower for x in ('/en_us/', '/ae2guide/', '/guide/', '/manual/', '/lexicon/'))
                        is_lang = (f_lower.endswith('en_us.json') and not is_json_book)
                        
                        if output_mode == "inplace" and target_file not in f_lower and f"/{lang_settings['file']}/" not in f_lower:
                            zout.writestr(item, zin.read(item))

                        if self.var_mods.get() and is_lang:
                            trans_filename = re.sub(r'en_us\.json$', target_file, item.filename, flags=re.IGNORECASE)
                            trans_t = trans_filename.lower()
                            
                            try: en_data = load_lenient_json(zin.read(item))
                            except: continue
                                
                            try: trans_data = load_lenient_json(zin.read(trans_files[trans_t])) if trans_t in trans_files else {}
                            except: trans_data = {}
                            
                            final_data = en_data.copy()
                            keys_to_translate = {}
                            
                            for k, en_text in en_data.items():
                                if not isinstance(en_text, str) or not en_text.strip(): continue
                                if is_technical_term(en_text):
                                    final_data[k] = en_text
                                    continue
                                if mode_overwrite == "append" and k in trans_data and isinstance(trans_data[k], str) and trans_data[k].strip():
                                    final_data[k] = trans_data[k]
                                    if final_data[k] == en_text and re.search(r'[a-zA-Z]', en_text): keys_to_translate[k] = en_text
                                elif re.search(r'[a-zA-Z]', en_text): keys_to_translate[k] = en_text

                            total_en = len([k for k, v in en_data.items() if isinstance(v, str) and re.search(r'[a-zA-Z]', v) and not is_technical_term(v)])
                            if total_en > 0:
                                if mode_overwrite == "skip" and (total_en - len(keys_to_translate)) >= total_en * 0.9:
                                    self.log_t("skip_interface", "yellow", name=mod_name)
                                    if output_mode == "resourcepack" and trans_t in trans_files and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            rp_zip_handle.writestr(trans_filename, zin.read(trans_files[trans_t]))
                                            written_files.add(trans_filename)
                                elif len(keys_to_translate) == 0 and mode_overwrite == "append":
                                    if output_mode == "resourcepack" and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            rp_zip_handle.writestr(trans_filename, json.dumps(final_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                            written_files.add(trans_filename)
                                    translated_any = True
                                else:
                                    self.log_t("translate_interface", "cyan", name=mod_name, count=len(keys_to_translate))
                                    trans_dict = self.translate_engine(keys_to_translate, engine, lang_settings, context_name=mod_name)
                                    for k, v in trans_dict.items(): final_data[k] = v
                                    out_data = json.dumps(final_data, ensure_ascii=False, indent=2).encode('utf-8')
                                    if output_mode == "resourcepack" and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            rp_zip_handle.writestr(trans_filename, out_data)
                                            written_files.add(trans_filename)
                                    else:
                                        if zout: zout.writestr(trans_filename, out_data)
                                        ru_files_written.add(trans_filename)
                                    translated_any = True

                        elif self.var_books.get() and is_json_book:
                            trans_filename = re.sub(r'/en_us/', f"/{lang_settings['file']}/", item.filename, flags=re.IGNORECASE)
                            trans_t = trans_filename.lower()
                            
                            try: en_data = load_lenient_json(zin.read(item))
                            except: continue
                                
                            try: trans_data = load_lenient_json(zin.read(trans_files[trans_t])) if trans_t in trans_files else {}
                            except: trans_data = {}
                            
                            en_strings = [s for s in extract_book_strings(en_data) if s.strip()]
                            trans_strings = [s for s in extract_book_strings(trans_data) if s.strip()] if trans_data else []
                            
                            keys_to_translate = {}
                            final_strings = []
                            
                            for i, en_s in enumerate(en_strings):
                                if is_technical_term(en_s):
                                    final_strings.append(en_s)
                                    continue
                                if mode_overwrite == "append" and i < len(trans_strings) and trans_strings[i].strip():
                                    final_strings.append(trans_strings[i])
                                    if trans_strings[i] == en_s and re.search(r'[a-zA-Z]', en_s): keys_to_translate[str(i)] = en_s
                                else:
                                    final_strings.append(en_s)
                                    if re.search(r'[a-zA-Z]', en_s): keys_to_translate[str(i)] = en_s

                            total_en = len([s for s in en_strings if re.search(r'[a-zA-Z]', s) and not is_technical_term(s)])
                            if total_en > 0:
                                if mode_overwrite == "skip" and (total_en - len(keys_to_translate)) >= total_en * 0.9:
                                    self.log_t("skip_book_json", "yellow", name=mod_name)
                                    if output_mode == "resourcepack" and trans_t in trans_files and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            rp_zip_handle.writestr(trans_filename, zin.read(trans_files[trans_t]))
                                            written_files.add(trans_filename)
                                elif len(keys_to_translate) == 0 and mode_overwrite == "append":
                                    if output_mode == "resourcepack" and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            inject_book_strings(en_data, iter(final_strings))
                                            rp_zip_handle.writestr(trans_filename, json.dumps(en_data, ensure_ascii=False, indent=2).encode('utf-8'))
                                            written_files.add(trans_filename)
                                    translated_any = True
                                else:
                                    self.log_t("translate_book_json", "magenta", name=mod_name, count=len(keys_to_translate))
                                    trans_dict = self.translate_engine(keys_to_translate, engine, lang_settings, context_name=mod_name)
                                    for i in range(len(final_strings)):
                                        if str(i) in trans_dict: final_strings[i] = trans_dict[str(i)]
                                    inject_book_strings(en_data, iter(final_strings))
                                    out_data = json.dumps(en_data, ensure_ascii=False, indent=2).encode('utf-8')
                                    if output_mode == "resourcepack" and rp_zip_handle:
                                        if trans_filename not in written_files:
                                            rp_zip_handle.writestr(trans_filename, out_data)
                                            written_files.add(trans_filename)
                                    else:
                                        if zout: zout.writestr(trans_filename, out_data)
                                        ru_files_written.add(trans_filename)
                                    translated_any = True

                        elif self.var_books.get() and is_md_book:
                            # =========================================================================
                            # ПУТИ КАК В РАБОЧЕМ РЕСУРСПАКЕ
                            # =========================================================================
                            if '/en_us/' in f_lower:
                                trans_filename = re.sub(r'/en_us/', f"/{lang_settings['file']}/", item.filename, flags=re.IGNORECASE)
                            else:
                                trans_filename = item.filename
                                
                            trans_t = trans_filename.lower()
                            l_regex = lang_settings.get('regex', r'[А-Яа-яЁё]')
                            
                            try: en_text = zin.read(item).decode('utf-8-sig', errors='ignore')
                            except: continue
                            try: trans_text = zin.read(trans_files[trans_t]).decode('utf-8-sig', errors='ignore') if trans_t in trans_files else ""
                            except: trans_text = ""
                            
                            en_lines = en_text.split('\n')
                            trans_lines = trans_text.split('\n') if trans_text else []
                            
                            keys_to_translate = {}
                            md_prefixes = {}
                            final_lines = []
                            
                            in_yaml_header = False
                            
                            for i, en_s in enumerate(en_lines):
                                s_stripped = en_s.strip()
                                
                                # ПАРСЕР ШАПКИ YAML (Где происходили краши AE2)
                                if s_stripped == '---':
                                    in_yaml_header = not in_yaml_header
                                    final_lines.append(en_s)
                                    if mode_overwrite == "append" and i < len(trans_lines) and trans_lines[i].strip() == '---':
                                        pass
                                    continue
                                    
                                if in_yaml_header:
                                    # Внутри шапки переводим ТОЛЬКО title:
                                    if s_stripped.lower().startswith('title:'):
                                        match = re.match(r'^(\s*title\s*:\s*[\'"]?)(.*?)([\'"]?)$', en_s, re.IGNORECASE)
                                        if match and re.search(r'[a-zA-Z]', match.group(2)):
                                            prefix, text_to_trans, suffix = match.groups()
                                            md_prefixes[str(i)] = (prefix, suffix)
                                            
                                            if mode_overwrite == "append" and i < len(trans_lines) and re.search(l_regex, trans_lines[i]):
                                                final_lines.append(trans_lines[i])
                                            else:
                                                final_lines.append(en_s) # Плейсхолдер
                                                keys_to_translate[str(i)] = text_to_trans
                                        else:
                                            final_lines.append(en_s)
                                    else:
                                        final_lines.append(en_s) # Оставляем системные команды на английском!
                                    continue
                                
                                # ОБРАБОТКА ОБЫЧНОГО ТЕКСТА
                                if s_stripped.startswith('<') or s_stripped.startswith('!['):
                                    final_lines.append(en_s)
                                    continue
                                    
                                if not s_stripped or not re.search(r'[a-zA-Z]', en_s) or is_technical_term(en_s):
                                    final_lines.append(en_s)
                                    continue
                                    
                                if mode_overwrite == "append" and i < len(trans_lines) and trans_lines[i].strip() and re.search(l_regex, trans_lines[i]):
                                    final_lines.append(trans_lines[i])
                                else:
                                    final_lines.append(en_s)
                                    keys_to_translate[str(i)] = en_s

                            total_en = len(keys_to_translate)
                            if total_en > 0:
                                self.log_t("translate_book_md", "magenta", name=mod_name, count=len(keys_to_translate))
                                trans_dict = self.translate_engine(keys_to_translate, engine, lang_settings, context_name=mod_name)
                                
                                for i_str, t_val in trans_dict.items():
                                    i_idx = int(i_str)
                                    if i_str in md_prefixes:
                                        p, s = md_prefixes[i_str]
                                        final_lines[i_idx] = p + t_val + s
                                    else:
                                        final_lines[i_idx] = t_val
                                        
                                out_data = '\n'.join(final_lines).encode('utf-8')
                                
                                if output_mode == "resourcepack" and rp_zip_handle:
                                    if trans_filename not in written_files:
                                        rp_zip_handle.writestr(trans_filename, out_data)
                                        written_files.add(trans_filename)
                                        # Гарантируем перезапись оригинала для AE2
                                        if trans_filename != item.filename and item.filename not in written_files:
                                            try: 
                                                rp_zip_handle.writestr(item.filename, out_data)
                                                written_files.add(item.filename)
                                            except: pass
                                else:
                                    if zout: zout.writestr(trans_filename, out_data)
                                    ru_files_written.add(trans_filename)
                                translated_any = True
                            else:
                                if mode_overwrite == "skip":
                                    self.log_t("skip_book_md", "yellow", name=mod_name)
                                if output_mode == "resourcepack" and rp_zip_handle:
                                    if trans_filename not in written_files:
                                        rp_zip_handle.writestr(trans_filename, '\n'.join(final_lines).encode('utf-8'))
                                        written_files.add(trans_filename)
                                elif output_mode == "inplace":
                                    if zout: zout.writestr(trans_filename, '\n'.join(final_lines).encode('utf-8'))
                                    ru_files_written.add(trans_filename)

                    if output_mode == "inplace" and zout:
                        for item in zin.infolist():
                            if (target_file in item.filename.lower() or f"/{lang_settings['file']}/" in item.filename.lower()) and item.filename not in ru_files_written:
                                try: zout.writestr(item, zin.read(item))
                                except: pass
                finally:
                    if zout: zout.close()

            if output_mode == "inplace":
                if translated_any and self.is_running: shutil.move(temp_filepath, filepath)
                else: os.remove(temp_filepath)
            else:
                if os.path.exists(temp_filepath): os.remove(temp_filepath)

        except Exception as e:
            if os.path.exists(temp_filepath): os.remove(temp_filepath)
            self.log_t("mod_error", "red", name=mod_name, error=e)

    def process_snbt(self, filepath, engine, mode_overwrite, lang_settings):
        if not self.var_quests.get(): return
        filename = os.path.basename(filepath)
        l_regex = lang_settings.get('regex', r'[А-Яа-яЁё]')
        bak_path = filepath + ".bak"
        if not os.path.exists(bak_path): shutil.copy2(filepath, bak_path)
        content_path = filepath if mode_overwrite == "append" else bak_path
            
        try:
            with open(content_path, 'r', encoding='utf-8') as f: content = f.read()
                
            strings_to_translate = []
            for m in re.finditer(r'(?:"|)(title|subtitle|text)(?:"|)\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.IGNORECASE):
                val = m.group(2)
                if val.strip() and not is_translation_key(val) and re.search(r'[a-zA-Z]', val): 
                    if mode_overwrite == "append" and re.search(l_regex, val): continue
                    strings_to_translate.append(val)
                
            for m in re.finditer(r'(?:"|)description(?:"|)\s*:\s*\[(.*?)\]', content, re.DOTALL | re.IGNORECASE):
                for str_m in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(1)):
                    val = str_m.group(1)
                    if val.strip() and not is_translation_key(val) and re.search(r'[a-zA-Z]', val): 
                        if mode_overwrite == "append" and re.search(l_regex, val): continue
                        strings_to_translate.append(val)
                    
            strings_to_translate = list(set(strings_to_translate))
            
            if len(strings_to_translate) == 0:
                if mode_overwrite == "append": self.log_t("quest_fully_append", "dim", name=filename)
                return
                
            if mode_overwrite == "skip":
                with open(filepath, 'r', encoding='utf-8') as f:
                    if re.search(l_regex, f.read()):
                        self.log_t("skip_quests", "yellow", name=filename)
                        return

            self.log_t("translate_quests", "yellow", name=filename, count=len(strings_to_translate))
            
            chunk_dict = {str(i): val for i, val in enumerate(strings_to_translate)}
            trans_dict = self.translate_engine(chunk_dict, engine, lang_settings, context_name=filename)
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
        except Exception as e: self.log_t("quest_error", "red", name=filename, error=e)

if __name__ == '__main__':
    app = TranslatorApp()
    app.mainloop()
