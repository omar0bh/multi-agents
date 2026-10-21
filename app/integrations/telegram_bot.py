import asyncio
import logging
import time
from typing import Any

import httpx

import re
from pathlib import Path

from app.agents.scout import AIScoutAgent
from app.agents.vision_verifier import DualModelVisionAgent
from app.bridge.manager import bridge_manager
from app.config import TELEGRAM_ALLOWED_USER_ID, TELEGRAM_BOT_TOKEN
from app.orchestration.manager import Orchestrator
from app.tools.email_sender import send_email
from app.tools.file_reader import delete_path, find_file, list_directory, read_text_file
from app.tools.gemini_audio import analyze_audio_with_gemini
from app.tools.git_tool import git_commit_and_push, git_status
from app.tools.image_downloader import download_web_image
from app.tools.project_scaffold import create_project_on_desktop
from app.tools.terminal_tool import run_safe_command

logger = logging.getLogger(__name__)

_GLOBAL_BOT: "TelegramEmployeeBot | None" = None


async def notify_owner(message: str, chat_id: int | None = None) -> bool:
    """Send a proactive notification to the owner's Telegram (e.g. when a PC task finishes)."""
    global _GLOBAL_BOT
    target_chat = chat_id
    if not target_chat and _GLOBAL_BOT and _GLOBAL_BOT.last_chat_id:
        target_chat = _GLOBAL_BOT.last_chat_id
    if not target_chat and TELEGRAM_ALLOWED_USER_ID and str(TELEGRAM_ALLOWED_USER_ID).isdigit():
        target_chat = int(TELEGRAM_ALLOWED_USER_ID)

    if not target_chat or not TELEGRAM_BOT_TOKEN:
        logger.warning("notify_owner: target_chat=%s or token missing", target_chat)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)]
    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            try:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": target_chat,
                        "text": chunk,
                        "parse_mode": "Markdown",
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Telegram Markdown rejected (%s). Resending plain.", resp.status_code)
                    await client.post(
                        url,
                        json={"chat_id": target_chat, "text": chunk},
                    )
            except Exception as e:
                logger.error("notify_owner send failed: %s", e)
                try:
                    await client.post(url, json={"chat_id": target_chat, "text": chunk})
                except Exception:
                    return False
    return True


class TelegramEmployeeBot:
    """Telegram Bot connecting incoming user messages to the DSPy Multi-Agent Orchestrator."""

    def __init__(self, token: str | None = None, allowed_user_id: str | None = None):
        global _GLOBAL_BOT
        self.token = token or TELEGRAM_BOT_TOKEN
        self.allowed_user_id = str(allowed_user_id or TELEGRAM_ALLOWED_USER_ID).strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.orchestrator = Orchestrator()
        from app.agents.discord_agent import DiscordArchitectAgent
        self.discord_agent = DiscordArchitectAgent()
        self.last_chat_id: int | None = None
        self._running = False
        _GLOBAL_BOT = self

    def is_configured(self) -> bool:
        return bool(self.token)

    async def send_document(self, chat_id: int, file_path: str | Path, caption: str = "") -> bool:
        """Send a real file/document from the PC to the user via Telegram."""
        if not self.base_url:
            return False

        path_obj = Path(file_path).expanduser().resolve()
        if not path_obj.is_file():
            return False

        url = f"{self.base_url}/sendDocument"
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                with open(path_obj, "rb") as f:
                    files = {"document": (path_obj.name, f)}
                    data = {"chat_id": chat_id, "caption": caption[:1024]}
                    resp = await client.post(url, data=data, files=files)
                    return resp.status_code == 200
            except Exception as exc:
                logger.error("Failed to send telegram document: %s", exc)
                return False

    async def send_photo(self, chat_id: int, file_path: str | Path, caption: str = "") -> bool:
        """Send a real photo from the PC to the user via Telegram."""
        if not self.base_url:
            return False

        path_obj = Path(file_path).expanduser().resolve()
        if not path_obj.is_file():
            return False

        url = f"{self.base_url}/sendPhoto"
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                with open(path_obj, "rb") as f:
                    files = {"photo": (path_obj.name, f)}
                    data = {"chat_id": chat_id, "caption": caption[:1024]}
                    resp = await client.post(url, data=data, files=files)
                    return resp.status_code == 200
            except Exception as exc:
                logger.error("Failed to send telegram photo: %s", exc)
                return False

    async def send_message(self, chat_id: int, text: str) -> bool:
        if not self.base_url:
            return False

        url = f"{self.base_url}/sendMessage"
        # Telegram max message length is 4096 characters; chunk if necessary
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        async with httpx.AsyncClient(timeout=30.0) as client:
            for chunk in chunks:
                try:
                    resp = await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "Markdown",
                        },
                    )
                    if resp.status_code != 200:
                        # Fallback to plain text if Telegram rejects markdown entities (like LaTeX/math)
                        logger.warning("Telegram Markdown rejected (%s). Resending as plain text.", resp.status_code)
                        await client.post(
                            url,
                            json={"chat_id": chat_id, "text": chunk},
                        )
                except Exception as exc:
                    # Fallback to plain text on exception
                    try:
                        logger.warning("Telegram send exception (%s). Retrying as plain text.", exc)
                        await client.post(
                            url,
                            json={"chat_id": chat_id, "text": chunk},
                        )
                    except Exception as err:
                        logger.error("Failed to send telegram message: %s", err)
                        return False
        return True

    async def handle_user_message(self, chat_id: int, text: str, user_id: int):
        # Security check:
        # If allowed_user_id is not set or contains non-digits (e.g. phone number), auto-bind to the owner's numeric Telegram ID
        if self.allowed_user_id and not self.allowed_user_id.isdigit():
            logger.info("Auto-binding numeric Telegram user_id=%s (replacing: %s)", user_id, self.allowed_user_id)
            self.allowed_user_id = str(user_id)
        elif not self.allowed_user_id:
            logger.info("Auto-binding first user_id=%s as allowed owner", user_id)
            self.allowed_user_id = str(user_id)
        elif str(user_id) != self.allowed_user_id:
            await self.send_message(
                chat_id,
                f"⚠️ عذراً، هذا البوت مخصص للاستخدام الشخصي فقط. معرفك: {user_id}",
            )
            return

        self.last_chat_id = chat_id
        text_stripped = text.strip()
        lowered = text_stripped.lower()

        # Command: Start / Help
        if text_stripped in ("/start", "/help"):
            welcome = (
                "👋 **أهلاً بك! أنا موظفك ومساعدك الذكي (DSPy Cloud AI Employee).**\n\n"
                "💼 **أبرز الأوامر التي يمكنك استخدامها من هاتفك 24/7:**\n"
                "• 🤖 `/scout` أو *'جيب لي نماذج AI مجانية'*: البحث وجلب أفضل نماذج AI المجانية (0$) مع روابط التسجيل بنقرة واحدة وتوزيع أدوارها.\n"
                "• 🌉 `/bridge` أو `/pc`: فحص حالة اتصال حاسوبك الشخصي (متصل Online أو مغلق Offline).\n"
                "• 🏗️ `/create اسم_المشروع`: إنشاء موقع أو تطبيق ويب كامل مباشرة على سطح المكتب.\n"
                "• 📁 `/file اسم_الملف`: إرسال أي ملف أو كود من حاسوبك إلى هاتفك مباشرة.\n"
                "• 📂 `/ls`: استعراض مجلدات وملفات المشروع أو سطح المكتب.\n"
                "• 🚀 `/status` و `/push`: فحص ومزامنة كود المشروع مع Git ومستودعك السحابي.\n"
                "• 📧 `/email`: إرسال رسائل بريد إلكتروني مع مرفقات تلقائياً عبر Gmail.\n"
                "• 📸 إرسال الصور: حل المسائل وتحليل الصور عبر الذكاء المزدوج (Gemini + Groq) بإجابات مختصرة ومباشرة.\n"
                "• ⚙️ `/run أمر`: تنفيذ أوامر على حاسوبك الشخصي بأمان عبر الجسر السحابي.\n"
                "• 📋 `/tasks`: عرض آخر 10 مهام في الطابور (معلقة، منجزة، ملغاة).\n"
                "• ❌ `/cancel <task_id>`: إلغاء مهمة معلقة قبل وصولها لحاسوبك.\n"
                "• 🧪 `/test` أو *'دير تيست للصلاحيات'*: فحص شامل وتشغيل تجريبي فوري لجميع أدوات ومهام النظام.\n"
                "• 🏛️ `/discord plan`: تصميم وهندسة سيرفر ديسكورد متكامل وتطبيقه تلقائياً بعد موافقتك."
            )
            await self.send_message(chat_id, welcome)
            return

        # Command: Discord Architect (/discord, or natural query)
        if text_stripped.startswith("/discord") or any(
            p in lowered for p in (
                "discord plan", "bni discord", "sawb discord", "discord server",
                "apply discord", "tb9 discord", "طبق ديسكورد", "طبق تصميم الديسكورد"
            )
        ):
            if "apply" in lowered or any(p in lowered for p in ("tb9", "taba9", "طبق", "bni", "ابني")):
                await self.send_message(chat_id, "⚙️ **جاري بناء وتنسيق سيرفر ديسكورد بالكامل...**\n⏳ إنشاء الرتب، القنوات، الأقسام، ونشر القوانين...")
                try:
                    loop = asyncio.get_running_loop()
                    res = await loop.run_in_executor(None, self.discord_agent.apply_current_plan)
                    if res.get("ok"):
                        await self.send_message(chat_id, res.get("summary"))
                    else:
                        await self.send_message(chat_id, f"❌ خطأ أثناء تطبيق التصميم: {res.get('error')}")
                except Exception as e:
                    logger.error("Discord apply error: %s", e)
                    await self.send_message(chat_id, f"⚠️ حدث خطأ أثناء البناء: `{e}`")
                return

            # Check for Clone Server Intent (/discord clone <ID> or kopi/clone)
            if "clone" in lowered or "copi" in lowered or "انسخ" in lowered or "نقل" in lowered:
                words = text_stripped.split()
                target_id = ""
                for w in words:
                    w_clean = w.strip("<>#:@,!?. ")
                    if w_clean.isdigit() and len(w_clean) >= 15:
                        target_id = w_clean
                        break

                if target_id:
                    await self.send_message(chat_id, f"🔍 **جاري فحص ونسخ هيكل السيرفر `{target_id}` بالكامل...**\n⏳ قراءة الرتب، القنوات، الأقسام، والصلاحيات...")
                    try:
                        loop = asyncio.get_running_loop()
                        res = await loop.run_in_executor(None, self.discord_agent.clone_server, target_id)
                        if res.get("ok"):
                            await self.send_message(chat_id, res.get("preview"))
                        else:
                            await self.send_message(chat_id, f"❌ {res.get('error')}")
                    except Exception as e:
                        logger.error("Discord clone error: %s", e)
                        await self.send_message(chat_id, f"⚠️ خطأ أثناء نسخ السيرفر: `{e}`")
                    return
                else:
                    await self.send_message(
                        chat_id,
                        "💡 **لنسخ سيرفر بواسطة الـ ID:**\n"
                        "يرجى كتابة الأمر مع ID السيرفر، مثال:\n"
                        "`/discord clone 1561491742367420416`\n\n"
                        "⚠️ *تذكير أمان:* يجب أن يكون البوت عضواً داخل ذلك السيرفر ليسمح ديسكورد بقراءته.\n"
                        "📸 *أو أرسل سكرين شوت لقنوات السيرفر وسيقوم الذكاء الاصطناعي بنسخها فوراً!*"
                    )
                    return

            # Otherwise, Generate or Preview Plan
            theme = "gaming"
            if any(k in lowered for k in ("code", "coding", "dev", "برمجة", "تطوير")):
                theme = "coding"

            await self.send_message(chat_id, f"🏛️ **جاري هندسة وتصميم سيرفر ديسكورد بنمط ({theme})...**\n⏳ تحليل القنوات والرتب المناسبة لبيئة المجتمع...")
            try:
                loop = asyncio.get_running_loop()
                res = await loop.run_in_executor(None, self.discord_agent.plan, theme)
                if res.get("ok"):
                    await self.send_message(chat_id, res.get("preview"))
                else:
                    await self.send_message(chat_id, f"❌ {res.get('error')}")
            except Exception as e:
                logger.error("Discord plan error: %s", e)
                await self.send_message(chat_id, f"⚠️ خطأ أثناء التخطيط: `{e}`")
            return

        # Command: System Self-Test & Diagnostics (/test, /diagnose, or natural query)
        if text_stripped.startswith(("/test", "/diag", "/check", "/doctor")) or any(
            p in lowered for p in (
                "dir test", "test lge3", "test tasks", "sla7yat", "sla7iat", "chof sla7yat",
                "test system", "system test", "diagnostic", "test lkoul", "test kaml", "testi", "test lya"
            )
        ):
            await self.send_message(chat_id, "🧪 **جاري تشغيل الفحص الآلي الشامل لجميع الوظائف والمهام والصلاحيات...**\n⏳ ثواني معدودة لفحص الجسر، الملفات، التيرمينال، الصوت، الذكاء الاصطناعي...")
            try:
                from app.tools.diagnostics import run_full_system_diagnostic, format_diagnostic_report
                loop = asyncio.get_running_loop()
                diag_results = await loop.run_in_executor(None, run_full_system_diagnostic)
                report_msg = format_diagnostic_report(diag_results)
                await self.send_message(chat_id, report_msg)
            except Exception as e:
                logger.error("Diagnostic error: %s", e)
                await self.send_message(chat_id, f"⚠️ خطأ أثناء الفحص الشامل: `{e}`")
            return

        # Command: Task Queue Viewer (/tasks)
        if text_stripped.startswith("/tasks") or any(
            p in lowered for p in ("chof tasks", "chof lmhmat", "l tasks", "show tasks", "liste tasks")
        ):
            tasks = bridge_manager.list_tasks(limit=10)
            if not tasks:
                await self.send_message(chat_id, "📋 **طابور المهام فارغ حالياً.**\nما كاين تا task معلقة.")
                return
            status_icons = {"pending": "⏳", "dispatched": "🔄", "completed": "✅", "cancelled": "❌"}
            lines = ["📋 **آخر 10 مهام في الطابور:**\n"]
            for t in tasks:
                icon = status_icons.get(t["status"], "❓")
                queued_ago = int(time.time() - t["queued_at"]) if t.get("queued_at") else 0
                lines.append(f"{icon} `{t['task_id']}` — **{t['action']}** ({t['status']}) منذ {queued_ago}ث")
            lines.append("\n💡 *لإلغاء مهمة معلقة:* `/cancel <task_id>`")
            await self.send_message(chat_id, "\n".join(lines))
            return

        # Command: Cancel a pending task (/cancel <task_id>)
        if text_stripped.startswith("/cancel"):
            parts = text_stripped.split()
            if len(parts) < 2:
                await self.send_message(chat_id, "⚠️ يرجى تحديد الـ task_id. مثال:\n`/cancel a1b2c3d4`\n\nاستخدم `/tasks` لعرض قائمة المهام.")
                return
            target_id = parts[1].strip()
            cancelled = bridge_manager.cancel_task(target_id)
            if cancelled:
                await self.send_message(chat_id, f"✅ **تم إلغاء المهمة `{target_id}` بنجاح!**\nلن تُنفَّذ على حاسوبك.")
            else:
                await self.send_message(chat_id, f"❌ لم يتم العثور على مهمة معلقة بالـ ID: `{target_id}`\nربما تم تنفيذها أو إلغاؤها مسبقاً. جرب `/tasks` لعرض الحالة.")
            return

        # Command: Bridge / PC Status (/bridge, /pc, or natural query)
        if text_stripped.startswith(("/bridge", "/pc")) or any(
            p in lowered for p in ("status pc", "chof pc", "wach pc tafi", "pc dyali", "etat pc", "bridge status")
        ):
            st = bridge_manager.get_status()
            if st.get("online"):
                ago = st.get("last_heartbeat_seconds_ago", 0)
                msg = (
                    f"🟢 **حاسوبك الشخصي متصل الآن (PC Online)!**\n\n"
                    f"💻 **اسم الجهاز:** `{st.get('pc_name')}`\n"
                    f"🖥️ **النظام:** `{st.get('os')}`\n"
                    f"📁 **المسار النشط:** `{st.get('desktop_dir')}`\n"
                    f"⏱️ **آخر إشارة (Heartbeat):** منذ {ago} ثانية\n"
                    f"📦 **المهام المعلقة:** {st.get('pending_tasks_count')} مهمة\n\n"
                    f"✅ *جميع الأوامر ستنفذ فوراً على حاسوبك.*"
                )
            else:
                msg = (
                    f"🔴 **حاسوبك الشخصي غير متصل حالياً (PC Offline)!**\n\n"
                    f"💤 **الحالة:** الجهاز مطفأ أو مفصول عن الإنترنت.\n"
                    f"☁️ **الخدمة السحابية (AWS):** تعمل بكامل طاقتها 24/7 (الذكاء الاصطناعي، البرمجة، البحث، البريد، تحليل الصور).\n"
                    f"📥 **المهام المحلية:** أي ملف أو مشروع تطلبه سيتم حفظه في الطابور وتنفيذه تلقائياً فور تشغيل حاسوبك!\n\n"
                    f"💡 *لتشغيل الجسر عند فتح الحاسوب، قم بتشغيل `scripts/start_bridge.bat`.*"
                )
            await self.send_message(chat_id, msg)
            return

        # Command: Scout Free AI Models (/scout or natural query)
        if text_stripped.startswith("/scout") or any(
            p in lowered for p in (
                "jibli ai", "free ai", "ai gratuit", "free api", "models gratuits",
                "scout", "namadij", "free models", "jibli free", "9leb 3la ai", "arani ai"
            )
        ):
            query = text_stripped.replace("/scout", "").strip() or "best free 0$ ai api models with high rate limit"
            await self.send_message(chat_id, "🕵️‍♂️ **جاري استكشاف الويب والمنتديات للبحث عن أفضل نماذج الذكاء الاصطناعي المجانية (0$)...**\n⏳ دقيقة واحدة لتحليل المواصفات وتحديد دور كل نموذج.")
            try:
                loop = asyncio.get_running_loop()
                scout_agent = AIScoutAgent()
                scout_res = await loop.run_in_executor(None, scout_agent.scout, query)
                report_text = scout_res.get("report", "تعذر توليد التقرير حالياً.")
                await self.send_message(
                    chat_id,
                    f"🏆 **تقرير المستكشف الذكي (Free AI Scout Report):**\n\n{report_text}"
                )
            except Exception as e:
                logger.error("Scout error: %s", e)
                await self.send_message(chat_id, f"⚠️ حدث خطأ أثناء فحص النماذج: `{e}`")
            return

        # Command: List files (/ls, /dir, or natural folder exploration in any language)
        # Understands: "chof folder X", "what inside X", "go to folder X", "list X", etc.
        is_ls_intent = text_stripped.startswith(("/ls", "/dir"))
        ls_folder_query = ""
        parent_loc = ""

        # Detect container category (Desktop, Downloads, Documents, etc.)
        if any(w in lowered for w in ("desktop", "bureau", "سطح المكتب")):
            parent_loc = "desktop"
        elif any(w in lowered for w in ("download", "downloads", "telechargement", "téléchargement", "التحميلات", "التنزيلات")):
            parent_loc = "downloads"
        elif any(w in lowered for w in ("document", "documents", "المستندات")):
            parent_loc = "documents"
        elif any(w in lowered for w in ("picture", "pictures", "image", "images", "photo", "photos", "الصور")):
            parent_loc = "pictures"
        elif any(w in lowered for w in ("video", "videos", "vedio", "vedios", "الفيديو", "فيديو")):
            parent_loc = "videos"
        elif any(w in lowered for w in ("music", "audio", "الموسيقى", "اموسيقى")):
            parent_loc = "music"

        if is_ls_intent:
            ls_folder_query = text_stripped.replace("/ls", "").replace("/dir", "").strip()
        elif any(p in lowered for p in (
            "what inside", "what's inside", "chof dakhel", "chof folder", "chof dossier",
            "list folder", "list directory", "show folder", "show me folder",
            "wach kayn", "chno kayn", "chno fi", "chno f ",
            "contents of", "inside the folder", "go to folder", "go to this folder",
            "told me what inside", "tell me what inside", "what is inside",
        )):
            is_ls_intent = True
            import re as _re

            # Non-greedy extraction stopping before location connectors or prompt trailers
            folder_patterns = [
                r'(?:folder|dossier|directory|مجلد|دوسي)\s+(?:named|called|smito|ismuhu)?\s*["\'«]?([^"\'\n\r,;?!]+?)["\'»]?(?:\s+(?:inside|in|on|fl|f|sur|wst|wast|dakhel|tolde|told|tell|show|what|chno|wach|and|3afak|please|svp)|\s*$)',
                r'(?:go to|open|chof|dkhoul l|adkhol)\s+(?:this\s+)?(?:folder\s+|dossier\s+)?["\'«]?([^"\'\n\r,;?!]+?)["\'»]?(?:\s+(?:inside|in|on|fl|f|sur|wst|wast|dakhel|tolde|told|tell|show|what|chno|wach|and|3afak|please|svp)|\s*$)',
                r'(?:inside|dakhel|fi|wst)\s+(?:folder\s+|dossier\s+)?["\'«]?([^"\'\n\r,;?!]+?)["\'»]?(?:\s+(?:folder|dossier|directory|tolde|told|tell|show|what)|\s*$)',
            ]
            for pat in folder_patterns:
                m = _re.search(pat, text_stripped, _re.IGNORECASE)
                if m:
                    cand = m.group(1).strip().strip("\"'., !?")
                    if cand and cand.lower() not in (
                        "folder", "dossier", "directory", "me", "it", "this", "the", "my",
                        "desktop", "downloads", "documents", "pictures", "videos", "music"
                    ):
                        ls_folder_query = cand
                        break

            if not ls_folder_query and not parent_loc:
                skip = {"folder","dossier","this","the","inside","what","chof","go","to","and",
                        "told","tell","show","list","me","it","my","is"}
                words = [w for w in text_stripped.split() if len(w) > 2 and w.lower() not in skip]
                if words:
                    ls_folder_query = words[-1]

        if is_ls_intent:
            import os as _os
            if _os.name != "nt":
                # Running on Cloud Linux (AWS) — must query the user's Local PC bridge!
                if bridge_manager.is_online:
                    target_display = ls_folder_query or parent_loc or "Desktop"
                    task_id = bridge_manager.queue_task(
                        "list_dir",
                        {"folder": ls_folder_query, "parent": parent_loc},
                        chat_id=chat_id,
                    )
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري استعراض محتويات المجلد من حاسوبك الشخصي عبر الجسر...**\n"
                        f"📂 المجلد المطلوب: `{target_display}`\n"
                        f"⏳ Task ID: `{task_id}` — ستصلك محتويات المجلد فوراً."
                    )
                    return
                else:
                    await self.send_message(
                        chat_id,
                        "📥 **حاسوبك الشخصي (Local PC) غير متصل حالياً بالجسر (Offline).**\n\n"
                        "💡 لاستعراض مجلدات حاسوبك وملفات سطح المكتب، تأكد من تشغيل:\n"
                        "`scripts/start_bridge.bat` على حاسوبك الشخصي.\n\n"
                        "أو اكتب `/bridge` لفحص حالة الاتصال."
                    )
                    return
            else:
                from pathlib import Path as _Path
                from app.tools.user_folders import get_known_user_folders as _get_folders
                from app.tools.file_reader import list_directory
                _known = _get_folders()
                search_bases = []
                if parent_loc and parent_loc in _known:
                    search_bases.append(_known[parent_loc])
                search_bases.extend([
                    _known["desktop"],
                    _known["documents"],
                    _known["downloads"],
                    _known["pictures"],
                    _known["videos"],
                    _known["music"],
                    _Path.home(),
                ])

                folder_to_list = None
                if ls_folder_query:
                    direct = _Path(ls_folder_query).expanduser()
                    if direct.is_dir():
                        folder_to_list = direct
                    elif ls_folder_query.lower() in _known:
                        folder_to_list = _known[ls_folder_query.lower()]
                    else:
                        for base in search_bases:
                            cand = base / ls_folder_query
                            if cand.is_dir():
                                folder_to_list = cand
                                break
                elif parent_loc and parent_loc in _known:
                    folder_to_list = _known[parent_loc]
                else:
                    folder_to_list = _known["desktop"]

                if folder_to_list and folder_to_list.is_dir():
                    items = list_directory(str(folder_to_list))
                    if items:
                        reply = f"📂 **محتويات `{folder_to_list.name}`** (`{folder_to_list}`):\n" + "\n".join(items)
                    else:
                        reply = f"📂 المجلد `{folder_to_list.name}` (`{folder_to_list}`) موجود على حاسوبك لكنه **فارغ**."
                else:
                    reply = (
                        f"❌ ما لقيت المجلد `{ls_folder_query or parent_loc}` على حاسوبك.\n\n"
                        f"💡 جرب:\n"
                        f"• `/ls Desktop` — سطح المكتب (`{_known.get('desktop')}`)\n"
                        f"• `/ls Documents` — المستندات\n"
                        f"• `/ls Downloads` — التنزيلات\n"
                        f"• أو مسار كامل مثل: `/ls C:\\Users\\DELL LATITUDE\\Desktop\\MyProject`"
                    )
                await self.send_message(chat_id, reply)
                return

        # Command: Get/Send File (/file, /get, or natural Arabic/Darija intent)
        is_file_request = False
        target_file_query = ""
        lowered = text_stripped.lower()

        if text_stripped.startswith(("/file", "/get", "/download")):
            is_file_request = True
            for prefix in ("/file", "/get", "/download"):
                if text_stripped.startswith(prefix):
                    target_file_query = text_stripped[len(prefix):].strip()
                    break
        elif any(verb in lowered for verb in ("jibli", "siftli", "jib li", "sift li", "send file", "get file", "arani file")) or (
            "file" in lowered and any(v in lowered for v in ("sift", "jib", "send", "download"))
        ):
            is_file_request = True
            parts = text_stripped.split()
            for part in parts:
                if "." in part and not part.startswith("http"):
                    target_file_query = part.strip(",.!?\"'")
                    break
            if not target_file_query and len(parts) >= 2:
                target_file_query = parts[-1]

        if is_file_request and target_file_query:
            import os as _os
            if _os.name != "nt":
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task(
                        "find_file",
                        {"query": target_file_query},
                        chat_id=chat_id,
                    )
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري البحث عن الملف `{target_file_query}` في حاسوبك الشخصي عبر الجسر...**\n"
                        f"📌 Task ID: `{task_id}` — ستصلك النتيجة فور العثور عليه."
                    )
                    return
                else:
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك الشخصي مغلق حالياً (Offline).**\n"
                        f"لتصفح أو تحميل ملفات حاسوبك الشخصي، قم بتشغيل الجسر على حاسوبك أولاً عبر `scripts/start_bridge.bat`."
                    )
                    return

            await self.send_message(chat_id, f"🔍 جاري البحث عن الملف `{target_file_query}` في حاسوبك...")
            found_path = find_file(target_file_query)
            if found_path and found_path.is_file():
                preview = ""
                try:
                    preview = read_text_file(str(found_path), max_chars=800)
                except Exception:
                    pass

                caption = f"📄 تم العثور على الملف: {found_path.name}"
                sent_doc = await self.send_document(chat_id, found_path, caption=caption)
                if sent_doc:
                    if preview:
                        await self.send_message(chat_id, f"👀 **معاينة سريعة لمحتوى الملف:**\n```\n{preview[:600]}\n```")
                    return
                else:
                    await self.send_message(chat_id, f"❌ تم العثور على الملف ولكن تعذر إرساله عبر تيليجرام (الحجم كبير أو غير مسموح). المسار:\n`{found_path}`")
                    return
            else:
                await self.send_message(
                    chat_id,
                    f"❌ لم أجد الملف `{target_file_query}` في مجلد المشروع أو سطح المكتب. تأكد من الاسم واكتب `/ls` لتصفح الملفات."
                )
                return

        # Command: Git Status
        if text_stripped in ("/status", "/git"):
            status_text = git_status()
            await self.send_message(chat_id, f"📁 **حالة المشروع (Git Status):**\n```\n{status_text}\n```")
            return

        # Command: Git Push
        if text_stripped.startswith("/push") or "push" in text_stripped.lower() and "git" in text_stripped.lower():
            await self.send_message(chat_id, "⏳ جاري حفظ التعديلات ودفعها إلى المستودع (Pushing to remote)...")
            msg = text_stripped.replace("/push", "").strip() or "Update via AI Telegram Employee"
            result = git_commit_and_push(msg)
            if result["ok"]:
                await self.send_message(chat_id, f"✅ تم الدفع بنجاح (Git Push Succeeded):\n`{result['detail']}`")
            else:
                await self.send_message(chat_id, f"❌ تعذر الـ Push:\nخطوة: {result['step']}\nتفاصيل: `{result['detail']}`")
            return

        # Command: Download/Search Web Image to PC
        # Detect requests like: "9alabli 3la tsswira dyal mr beast fl web o instaliha o 7dthali f dossier wst vedio folder"
        image_keywords = ("tsswira", "tswira", "tasswira", "tsawer", "image", "photo", "picture", "صورة", "صور")
        action_keywords = ("9alab", "search", "jib", "telechargi", "download", "instali", "save", "7ot", "7dtha", "dir", "نزل", "حمل", "احفظ", "جيب", "ابحث")

        has_image_kw = any(k in lowered for k in image_keywords)
        has_action_kw = any(k in lowered for k in action_keywords)

        if text_stripped.startswith(("/image", "/img", "/download_img")) or (
            has_image_kw and has_action_kw and any(w in lowered for w in ("desktop", "bureau", "folder", "dossier", "vedio", "video", "web", "fl web", "f "))
        ):
            # Determine target folder category based on user message (Videos, Pictures, Documents, Desktop, etc.)
            if any(w in lowered for w in ("vedio", "video", "videos", "vidéo", "فيديو", "فيديوهات")):
                folder = "videos"
                folder_label = "الفيديوهات (Videos)"
            elif any(w in lowered for w in ("picture", "pictures", "photo", "photos", "الصور", "صور")):
                folder = "pictures"
                folder_label = "الصور (Pictures)"
            elif any(w in lowered for w in ("document", "documents", "doc", "docs", "مستند", "مستندات")):
                folder = "documents"
                folder_label = "المستندات (Documents)"
            elif any(w in lowered for w in ("download", "downloads", "telechargement", "تنزيلات")):
                folder = "downloads"
                folder_label = "التنزيلات (Downloads)"
            elif any(w in lowered for w in ("desktop", "bureau", "biro", "سطح المكتب")):
                folder = "desktop"
                folder_label = "سطح المكتب (Desktop)"
            else:
                folder = "pictures"
                folder_label = "الصور (Pictures)"

            # Extract search query
            patterns = [
                r'(?:dyal|de|of|صورة|صوره|image of|photo de|tswira dyal|tsswira dyal)\s+([^,]+?)(?:\s+(?:fl|f|wst|on|in|au|sur|folder|dossier|web|o\s+|et\s+|and\s+|w\s+)|$)',
                r'(?:download|instali|telechargi|حمل|نزل|جيب|ابحث عن)\s+([^,]+?)(?:\s+(?:fl|f|wst|on|in|au|sur|folder|dossier|web)|$)',
            ]
            query = ""
            for p in patterns:
                m = re.search(p, text_stripped, re.IGNORECASE)
                if m:
                    cand = m.group(1).strip()
                    cand = re.sub(r'^(?:tsswira|tswira|image|photo|صورة)\s+(?:dyal|de|of)?\s*', '', cand, flags=re.IGNORECASE).strip()
                    if cand and cand.lower() not in ('web', 'desktop', 'folder', 'dossier'):
                        query = cand
                        break
            if not query:
                parts = text_stripped.split()
                query = parts[-1] if parts else "image"

            import os
            # If deployed in the cloud (Linux), dispatch to Local PC Bridge if available
            if os.name != "nt":
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task("download_image", {"query": query, "folder": folder}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري إرسال المهمة إلى حاسوبك الشخصي المتصل...**\n"
                        f"🔍 جاري البحث في الويب عن صورة `{query}` وتحميلها وحفظها داخل مجلد `{folder_label}` على حاسوبك (Task #{task_id})."
                    )
                    return
                else:
                    task_id = bridge_manager.queue_task("download_image", {"query": query, "folder": folder}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك الشخصي مغلق حالياً (Offline).**\n\n"
                        f"تم حفظ أمر البحث وتحميل صورة `{query}` داخل مجلد `{folder_label}` في طابور المهام (Task #{task_id}).\n"
                        f"⚡ بمجرد تشغيل حاسوبك، سيقوم الجسر بتحميلها فوراً وإشعارك هنا!"
                    )
                    return

            # Running locally on PC
            await self.send_message(chat_id, f"🔍 جاري البحث في الويب عن صورة `{query}` وتحميلها على حاسوبك...")
            dl_res = download_web_image(query, folder_name=folder)
            if dl_res.get("ok"):
                saved_path = Path(dl_res.get("path"))
                caption = f"✅ تم تحميل الصورة بنجاح وحفظها في:\n`{saved_path}`"
                # Send photo preview to Telegram chat
                sent = await self.send_photo(chat_id, saved_path, caption=caption)
                if not sent:
                    await self.send_document(chat_id, saved_path, caption=caption)
                return
            else:
                await self.send_message(chat_id, f"❌ حدث خطأ أثناء تحميل الصورة: {dl_res.get('error')}")
                return

        # Command: File & Folder Operations (Copy, Move, Rename, Create Folder, etc.)
        from app.tools.intent_parser import parse_file_operation_intent
        file_intent = await parse_file_operation_intent(text_stripped)

        if file_intent.get("is_file_op"):
            if file_intent.get("error"):
                await self.send_message(chat_id, f"⚠️ {file_intent.get('error')}")
                return

            action = file_intent.get("action")
            params = {
                "src": file_intent.get("src", ""),
                "path": file_intent.get("src", ""),
                "target": file_intent.get("src", ""),
                "src_parent": file_intent.get("src_parent", ""),
                "dest_folder": file_intent.get("dest_folder", "desktop"),
                "dest_name": file_intent.get("dest_name", ""),
                "is_folder": file_intent.get("is_folder", False),
                "folder": file_intent.get("folder", file_intent.get("src", "")),
                "parent": file_intent.get("parent", "desktop"),
            }
            lbl = file_intent.get("label_ar") or f"تنفيذ {action}"

            import os as _os
            if _os.name != "nt":
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task(action, params, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري تنفيذ العملية على حاسوبك الشخصي عبر الجسر...**\n"
                        f"⚙️ {lbl}\n"
                        f"⏳ Task ID: `{task_id}` — ستصلك النتيجة فور اكتمالها."
                    )
                    return
                else:
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك الشخصي غير متصل حالياً بالجسر (Offline).**\n\n"
                        f"💡 لإجراء عمليات الملفات على حاسوبك، يرجى تشغيل الجسر عبر الضغط على:\n"
                        f"`scripts/start_bridge.bat` على حاسوبك الشخصي."
                    )
                    return
            else:
                from scripts.local_bridge import execute_local_task
                res = execute_local_task(action, params)
                if res.get("ok"):
                    await self.send_message(chat_id, f"✅ **تحديث من حاسوبك الشخصي:**\n{res.get('detail')}")
                else:
                    await self.send_message(chat_id, f"❌ **خطأ:**\n{res.get('error')}")
                return

        # Command: Create Project / Website / Scaffold on Desktop
        is_create_intent = (
            text_stripped.startswith(("/create", "/new", "/make"))
            or (
                any(p in lowered for p in ("create", "sawb ", "creer ", "créer ", "make "))
                and any(k in lowered for k in ("project", "machro3", "site", "website", "app", "application"))
            )
            or any(
                p in lowered for p in (
                    "create project", "dir machro3", "cree projet", "sawb site",
                    "creer site", "create website", "create site", "sawb li site"
                )
            )
        )

        if is_create_intent:
            name = "my_new_project"
            parts = text_stripped.split()
            if text_stripped.startswith("/create") and len(parts) > 1:
                name = parts[1]
            else:
                for idx, part in enumerate(parts):
                    clean_part = part.lower().strip(",.!?\"':;")
                    if clean_part in ("samih", "smih", "name", "named", "project", "machro3", "file", "folder", "dossier") and idx + 1 < len(parts):
                        cand = parts[idx + 1].strip(",.!?\"':;")
                        if cand.lower() not in ("on", "in", "f", "fl", "desktop", "bureau"):
                            name = cand
                            break
                if name in ("my_new_project", "on", "in", "desktop", "bureau", "file", "folder"):
                    name = parts[-1].strip(",.!?\"':;")

            # Determine target user folder for project creation (Desktop, Documents, Videos, etc.)
            if any(w in lowered for w in ("vedio", "video", "videos", "vidéo", "فيديو", "فيديوهات")):
                target_loc = "videos"
                loc_label = "الفيديوهات (Videos)"
            elif any(w in lowered for w in ("document", "documents", "doc", "docs", "مستند", "مستندات", "المستندات")):
                target_loc = "documents"
                loc_label = "المستندات (Documents)"
            elif any(w in lowered for w in ("picture", "pictures", "photo", "photos", "الصور", "صور")):
                target_loc = "pictures"
                loc_label = "الصور (Pictures)"
            elif any(w in lowered for w in ("download", "downloads", "telechargement", "تنزيلات")):
                target_loc = "downloads"
                loc_label = "التنزيلات (Downloads)"
            elif any(w in lowered for w in ("music", "audio", "موسيقى")):
                target_loc = "music"
                loc_label = "الموسيقى (Music)"
            else:
                target_loc = "desktop"
                loc_label = "سطح المكتب (Desktop)"

            # If deployed in the cloud (Linux), dispatch to Local PC Bridge if available
            import os
            if os.name != "nt":
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task("create_project", {"name": name, "target_location": target_loc}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري إرسال الأمر إلى حاسوبك الشخصي المتصل...**\n"
                        f"🏗️ سيتم إنشاء مشروع `{name}` داخل `{loc_label}` الآن عبر الجسر السحابي (Task #{task_id})."
                    )
                    return
                else:
                    task_id = bridge_manager.queue_task("create_project", {"name": name, "target_location": target_loc}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك الشخصي مغلق حالياً (Offline).**\n\n"
                        f"تم حفظ أمر إنشاء مشروع `{name}` داخل `{loc_label}` في طابور المهام (Task #{task_id}).\n"
                        f"⚡ بمجرد تشغيل حاسوبك، سيقوم الجسر بإنشائه فوراً وإشعارك هنا!"
                    )
                    return

            # Running locally on PC
            await self.send_message(chat_id, f"🏗️ جاري إنشاء المشروع `{name}` داخل `{loc_label}`...")
            res = create_project_on_desktop(name, target_location=target_loc)
            if res.get("ok"):
                files_str = "\n".join(f"- `{f}`" for f in res.get("files", []))
                await self.send_message(
                    chat_id,
                    f"🎉 **تم إنشاء المشروع بنجاح داخل `{loc_label}`!**\n\n"
                    f"📁 **المسار:** `{res.get('path')}`\n"
                    f"📄 **الملفات التي تم إنشاؤها:**\n{files_str}\n\n"
                    f"💡 *يمكنك فتحه وتشغيله مباشرة على حاسوبك أو تعديله من هاتفك!*"
                )
            else:
                await self.send_message(chat_id, f"❌ حدث خطأ أثناء إنشاء المشروع: {res.get('error')}")
            return

        # Command: Send Email (/email or natural intent)
        if text_stripped.startswith("/email") or any(p in lowered for p in ("sift email", "send email", "sift mail", "send mail", "envoyer email")):
            emails = re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", text_stripped)
            if not emails:
                await self.send_message(
                    chat_id,
                    "⚠️ يرجى تحديد البريد الإلكتروني للمستلم. مثال:\n`/email hr@company.com أريد التقديم على الوظيفة`"
                )
                return

            recipient = emails[0]
            # Check for attachment
            att_path = None
            for word in text_stripped.split():
                if "." in word and any(word.endswith(ext) for ext in (".pdf", ".docx", ".txt", ".png")):
                    found = find_file(word.strip(",.!?\"'"))
                    if found:
                        att_path = str(found)
                        break

            body_text = text_stripped
            subject = "Message from AI Employee"

            await self.send_message(chat_id, f"📧 جاري إرسال البريد الإلكتروني إلى `{recipient}`...")
            email_res = send_email(to_email=recipient, subject=subject, body=body_text, attachment_path=att_path)
            if email_res.get("ok"):
                att_note = f"\n📎 المرفق: `{att_path}`" if att_path else ""
                await self.send_message(chat_id, f"✅ **تم إرسال البريد بنجاح!**\nإلى: `{recipient}`{att_note}")
            else:
                await self.send_message(
                    chat_id,
                    f"❌ **تعذر إرسال الإيميل:**\n{email_res.get('error')}\n\n"
                    f"💡 *تأكد من إضافة `SMTP_EMAIL` و `SMTP_PASSWORD` في ملف `.env` (استخدم Google App Password لحساب Gmail).*"
                )
            return

        # Command: Delete File / Folder (/delete or natural intent)
        if text_stripped.startswith(("/delete", "/del", "/rm")) or any(
            p in lowered for p in ("delete ", "delete", "mse7 ", "mse7li", "supprimer ")
        ):
            target = ""
            for pfx in ("/delete", "/del", "/rm"):
                if text_stripped.startswith(pfx):
                    target = text_stripped[len(pfx):].strip()
                    break
            if not target:
                parts = text_stripped.split()
                if len(parts) >= 2:
                    target = parts[-1].strip(",.!?\"'")

            if target:
                await self.send_message(chat_id, f"🗑️ جاري حذف `{target}` من حاسوبك...")
                del_res = delete_path(target)
                if del_res.get("ok"):
                    await self.send_message(chat_id, f"✅ **تم حذف `{del_res.get('name')}` بنجاح من الحاسوب!**")
                else:
                    await self.send_message(chat_id, f"❌ **تعذر الحذف:** {del_res.get('error')}")
                return

        # Command: Run Safe Terminal Command (/cmd, /terminal, /run)
        # SECURITY: On cloud (Linux), always routes through the local PC bridge.
        # Direct execution on the AWS server is intentionally disabled.
        if text_stripped.startswith(("/cmd", "/terminal", "/run")) or any(
            text_stripped.lower().startswith(p) for p in ("install ", "instali ", "npm ", "pip ", "winget ", "git clone")
        ):
            cmd = ""
            for pfx in ("/cmd", "/terminal", "/run"):
                if text_stripped.startswith(pfx):
                    cmd = text_stripped[len(pfx):].strip()
                    break
            if not cmd:
                cmd = text_stripped

            import os as _os
            if _os.name != "nt":
                # Running on cloud Linux — always delegate to Local PC bridge
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task("run_command", {"cmd": cmd}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري إرسال الأمر إلى حاسوبك الشخصي عبر الجسر...**\n"
                        f"⚙️ `{cmd}`\n"
                        f"📌 Task ID: `{task_id}` — ستصلك النتيجة فور الانتهاء."
                    )
                else:
                    task_id = bridge_manager.queue_task("run_command", {"cmd": cmd}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك غير متصل حالياً.**\n"
                        f"تم حفظ الأمر `{cmd}` في الطابور (Task `{task_id}`).\n"
                        f"⚡ سيُنفَّذ تلقائياً فور تشغيل الجسر!"
                    )
                return

            # Running directly on local Windows PC
            await self.send_message(chat_id, f"⚙️ **جاري تنفيذ الأمر في التيرمنال بأمان:**\n`{cmd}`")
            exec_res = run_safe_command(cmd)
            if exec_res.get("blocked"):
                await self.send_message(chat_id, f"🛡️ **تم حظر الأمر تلقائياً لحماية النظام:**\n{exec_res.get('error')}")
                return

            if exec_res.get("ok"):
                output = exec_res.get("stdout") or "Command completed with no output."
                await self.send_message(chat_id, f"✅ **تم التنفيذ بنجاح:**\n```\n{output[:3500]}\n```")
            else:
                err = exec_res.get("stderr") or exec_res.get("error") or "Non-zero exit code."
                await self.send_message(chat_id, f"⚠️ **فشل الأمر أو خرج بخطأ:**\n```\n{err[:3500]}\n```")
            return

        # Standard Multi-Agent Execution
        self.last_chat_id = chat_id
        await self.send_message(chat_id, "🤖 استلمت طلبك وجاري تحليله وتوجيهه للوكيل المختص...")

        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self.orchestrator.run, text_stripped)

            workflow = res.get("workflow", "general")
            result_data = res.get("result", {})

            # Format the output nicely
            if isinstance(result_data, dict) and "answer" in result_data and len(result_data) == 1:
                reply_text = f"💡 **الإجابة:**\n\n{result_data['answer']}"
            else:
                lines = [f"🎯 **الوكيل المنفذ:** `{workflow}`\n"]
                if isinstance(result_data, dict):
                    for k, v in result_data.items():
                        if isinstance(v, (str, int, float, bool)):
                            lines.append(f"**{k.capitalize()}:**\n{v}\n")
                        elif isinstance(v, list) and v and isinstance(v[0], dict):
                            lines.append(f"**{k.capitalize()}:**")
                            for item in v[:5]:
                                lines.append(f"- [{item.get('title', 'Link')}]({item.get('url', '')})")
                else:
                    lines.append(str(result_data))
                reply_text = "\n".join(lines)
            await self.send_message(chat_id, reply_text)

        except Exception as exc:
            logger.exception("Error processing message in Telegram bot: %s", exc)
            await self.send_message(chat_id, f"❌ حدث خطأ أثناء تنفيذ المهمة: {exc}")

    async def handle_user_photo(self, chat_id: int, file_id: str, caption: str, user_id: int):
        """Process photos with Gemini Vision, cross-verified and fact-checked by Groq."""
        if self.allowed_user_id and not self.allowed_user_id.isdigit():
            self.allowed_user_id = str(user_id)
        elif not self.allowed_user_id:
            self.allowed_user_id = str(user_id)
        elif str(user_id) != self.allowed_user_id:
            await self.send_message(chat_id, f"⚠️ عذراً، هذا البوت مخصص للاستخدام الشخصي فقط. معرفك: {user_id}")
            return

        self.last_chat_id = chat_id
        await self.send_message(
            chat_id,
            "👁️ **استلمت الصورة!**\n"
            "1️⃣ جاري تحليل الصورة بواسطة **Gemini Vision**...\n"
            "2️⃣ ثم تمرير التحليل إلى **Groq (120B)** لمراجعته والتحقق من صحته الرياضية والمنطقية..."
        )

        try:
            # 1. Download photo from Telegram
            async with httpx.AsyncClient(timeout=45.0) as client:
                info_resp = await client.get(f"{self.base_url}/getFile", params={"file_id": file_id})
                if info_resp.status_code != 200:
                    await self.send_message(chat_id, "❌ تعذر جلب مسار الصورة من سيرفر تيليجرام.")
                    return
                file_path = info_resp.json().get("result", {}).get("file_path")
                file_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
                img_resp = await client.get(file_url)
                if img_resp.status_code != 200:
                    await self.send_message(chat_id, "❌ تعذر تحميل محتوى الصورة.")
                    return
                image_bytes = img_resp.content

            # 2. Run Dual-Model Processing (Gemini Vision -> Groq Fact-Checker)
            agent = DualModelVisionAgent()
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, agent.process_and_verify, image_bytes, caption)

            if not res.get("ok"):
                await self.send_message(chat_id, f"❌ خطأ أثناء معالجة الصورة: {res.get('error')}")
                return

            reply = (
                f"🎯 **الأجوبة المعتمدة:**\n\n"
                f"{res.get('final_answer')}\n\n"
                f"───────────────────\n"
                f"✅ *تم الفحص والتدقيق المزدوج بواسطة: Gemini 3.8 + Groq 120B*"
            )
            await self.send_message(chat_id, reply)

        except Exception as exc:
            logger.exception("Error processing photo: %s", exc)
            await self.send_message(chat_id, f"❌ حدث خطأ أثناء تحليل وتدقيق الصورة: {exc}")

    async def handle_user_audio(self, chat_id: int, file_id: str, mime_type: str, user_id: int):
        """Process voice notes and audio messages directly with Gemini Audio."""
        if self.allowed_user_id and not self.allowed_user_id.isdigit():
            self.allowed_user_id = str(user_id)
        elif not self.allowed_user_id:
            self.allowed_user_id = str(user_id)
        elif str(user_id) != self.allowed_user_id:
            await self.send_message(chat_id, f"⚠️ عذراً، هذا البوت مخصص للاستخدام الشخصي فقط. معرفك: {user_id}")
            return

        self.last_chat_id = chat_id
        await self.send_message(
            chat_id,
            "🎙️ **استلمت رسالتك الصوتية!**\n"
            "⏳ جاري الاستماع إليها وتحويلها إلى نص فوري عبر **Groq Whisper**..."
        )

        try:
            # 1. Download audio file from Telegram
            async with httpx.AsyncClient(timeout=45.0) as client:
                info_resp = await client.get(f"{self.base_url}/getFile", params={"file_id": file_id})
                if info_resp.status_code != 200:
                    await self.send_message(chat_id, "❌ تعذر جلب مسار التسجيل الصوتي من سيرفر تيليجرام.")
                    return
                file_path = info_resp.json().get("result", {}).get("file_path")
                file_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
                audio_resp = await client.get(file_url)
                if audio_resp.status_code != 200:
                    await self.send_message(chat_id, "❌ تعذر تحميل محتوى التسجيل الصوتي.")
                    return
                audio_bytes = audio_resp.content

            # 2. Transcribe with ultra-fast Groq Whisper
            from app.tools.audio_transcriber import transcribe_audio_with_groq
            loop = asyncio.get_running_loop()
            trans_res = await loop.run_in_executor(None, transcribe_audio_with_groq, audio_bytes, "voice.ogg")

            if trans_res.get("ok") and trans_res.get("text"):
                spoken_text = trans_res.get("text").strip()
                await self.send_message(chat_id, f"🎙️ **سمعتك قلتي:**\n`\"{spoken_text}\"`\n\n⚡ جاري تنفيذ طلبك فوراً...")
                # Pass the transcribed text into the normal agent/action pipeline!
                await self.handle_user_message(chat_id, spoken_text, user_id)
                return

            # Fallback to Gemini Audio if Groq failed
            res = await loop.run_in_executor(
                None,
                analyze_audio_with_gemini,
                audio_bytes,
                "Listen carefully to this audio/voice note. Transcribe what the user said, and answer their question or request directly, clearly, and helpfully in the exact same language they used (Darija, Arabic, English, or French).",
                mime_type,
            )

            if not res.get("ok"):
                await self.send_message(chat_id, f"❌ خطأ أثناء معالجة الصوت: {trans_res.get('error') or res.get('error')}")
                return

            reply = f"🎙️ **رد المساعد الذكي:**\n\n{res.get('text')}"
            await self.send_message(chat_id, reply)

        except Exception as exc:
            logger.exception("Error processing audio: %s", exc)
            await self.send_message(chat_id, f"❌ حدث خطأ أثناء معالجة التسجيل الصوتي: {exc}")

    async def start_polling(self):
        if not self.is_configured():
            logger.info("Telegram Bot Token is not set. Telegram integration disabled.")
            return

        logger.info("Starting Telegram Employee Bot polling loop...")
        self._running = True
        offset = 0

        async with httpx.AsyncClient(timeout=45.0) as client:
            while self._running:
                try:
                    url = f"{self.base_url}/getUpdates"
                    resp = await client.get(url, params={"offset": offset, "timeout": 20})
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            message = update.get("message")
                            if message and "text" in message:
                                chat_id = message["chat"]["id"]
                                user_id = message["from"]["id"]
                                text = message["text"]
                                asyncio.create_task(self.handle_user_message(chat_id, text, user_id))
                            elif message and "photo" in message:
                                chat_id = message["chat"]["id"]
                                user_id = message["from"]["id"]
                                file_id = message["photo"][-1]["file_id"]
                                caption = message.get("caption") or "قم بحل أو تحليل ما في هذه الصورة بالتفصيل."
                                asyncio.create_task(self.handle_user_photo(chat_id, file_id, caption, user_id))
                            elif message and ("voice" in message or "audio" in message):
                                chat_id = message["chat"]["id"]
                                user_id = message["from"]["id"]
                                voice_obj = message.get("voice") or message.get("audio")
                                file_id = voice_obj.get("file_id")
                                mime_type = voice_obj.get("mime_type", "audio/ogg")
                                asyncio.create_task(self.handle_user_audio(chat_id, file_id, mime_type, user_id))
                    elif resp.status_code in (401, 404):
                        logger.error("Invalid Telegram Bot Token. Stopping bot.")
                        break
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("Telegram polling error: %s", exc)
                    await asyncio.sleep(5)


async def main():
    bot = TelegramEmployeeBot()
    if not bot.is_configured():
        print("Please set TELEGRAM_BOT_TOKEN in .env")
        return
    print("Telegram Employee Bot is running. Press Ctrl+C to stop.")
    await bot.start_polling()


if __name__ == "__main__":
    asyncio.run(main())
