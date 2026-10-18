import asyncio
import logging
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
from app.tools.git_tool import git_commit_and_push, git_status
from app.tools.project_scaffold import create_project_on_desktop
from app.tools.terminal_tool import run_safe_command

logger = logging.getLogger(__name__)

_GLOBAL_BOT: "TelegramEmployeeBot | None" = None


async def notify_owner(message: str) -> bool:
    """Send a proactive notification to the owner's Telegram (e.g. when a PC task finishes)."""
    global _GLOBAL_BOT
    if _GLOBAL_BOT and _GLOBAL_BOT.last_chat_id:
        return await _GLOBAL_BOT.send_message(_GLOBAL_BOT.last_chat_id, message)
    return False


class TelegramEmployeeBot:
    """Telegram Bot connecting incoming user messages to the DSPy Multi-Agent Orchestrator."""

    def __init__(self, token: str | None = None, allowed_user_id: str | None = None):
        global _GLOBAL_BOT
        self.token = token or TELEGRAM_BOT_TOKEN
        self.allowed_user_id = str(allowed_user_id or TELEGRAM_ALLOWED_USER_ID).strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.orchestrator = Orchestrator()
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

    async def send_message(self, chat_id: int, text: str) -> bool:
        if not self.base_url:
            return False

        url = f"{self.base_url}/sendMessage"
        # Telegram max message length is 4096 characters; chunk if necessary
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        async with httpx.AsyncClient(timeout=30.0) as client:
            for chunk in chunks:
                try:
                    await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "Markdown",
                        },
                    )
                except Exception:
                    # Fallback to plain text if markdown formatting fails
                    try:
                        await client.post(
                            url,
                            json={"chat_id": chat_id, "text": chunk},
                        )
                    except Exception as exc:
                        logger.error("Failed to send telegram message: %s", exc)
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

        text_stripped = text.strip()

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
                "• ⚙️ `/run أمر`: تنفيذ أوامر برمجية وتثبيت مكتبات بأمان وحماية لنظامك."
            )
            await self.send_message(chat_id, welcome)
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

        # Command: List files (/ls, /dir)
        if text_stripped.startswith(("/ls", "/dir")):
            folder = text_stripped.replace("/ls", "").replace("/dir", "").strip() or "."
            items = list_directory(folder)
            if items:
                reply = f"📂 **محتويات المجلد ({folder}):**\n" + "\n".join(items)
            else:
                reply = f"❌ لم يتم العثور على المجلد أو أنه فارغ: `{folder}`"
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
            # Extract possible file name
            parts = text_stripped.split()
            for part in parts:
                if "." in part and not part.startswith("http"):
                    target_file_query = part.strip(",.!?\"'")
                    break
            if not target_file_query and len(parts) >= 2:
                target_file_query = parts[-1]

        if is_file_request and target_file_query:
            await self.send_message(chat_id, f"🔍 جاري البحث عن الملف `{target_file_query}` في حاسوبك...")
            found_path = find_file(target_file_query)
            if found_path and found_path.is_file():
                # Read short preview if text
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

        # Command: Create Project / Website on Desktop
        if text_stripped.startswith("/create") or any(
            p in lowered for p in ("create project", "dir machro3", "cree projet", "sawb site", "creer site", "create website", "create site", "sawb li site")
        ):
            name = "my_new_project"
            parts = text_stripped.split()
            if text_stripped.startswith("/create") and len(parts) > 1:
                name = parts[1]
            else:
                for idx, part in enumerate(parts):
                    if part.lower() in ("samih", "smih", "name", "project", "machro3") and idx + 1 < len(parts):
                        name = parts[idx + 1].strip(",.!?\"'")
                        break

            # If deployed in the cloud (Linux), dispatch to Local PC Bridge if available
            import os
            if os.name != "nt":
                if bridge_manager.is_online:
                    task_id = bridge_manager.queue_task("create_project", {"name": name}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"🌉 **جاري إرسال الأمر إلى حاسوبك الشخصي المتصل...**\n"
                        f"🏗️ سيتم إنشاء مشروع `{name}` على سطح المكتب الآن عبر الجسر السحابي (Task #{task_id})."
                    )
                    return
                else:
                    task_id = bridge_manager.queue_task("create_project", {"name": name}, chat_id=chat_id)
                    await self.send_message(
                        chat_id,
                        f"📥 **حاسوبك الشخصي مغلق حالياً (Offline).**\n\n"
                        f"تم حفظ أمر إنشاء مشروع `{name}` في طابور المهام (Task #{task_id}).\n"
                        f"⚡ بمجرد تشغيل حاسوبك، سيقوم الجسر بإنشائه على سطح المكتب فوراً وإشعارك هنا!"
                    )
                    return

            # Running locally on PC
            await self.send_message(chat_id, f"🏗️ جاري إنشاء المشروع `{name}` على سطح المكتب (Desktop)...")
            res = create_project_on_desktop(name)
            if res.get("ok"):
                files_str = "\n".join(f"- `{f}`" for f in res.get("files", []))
                await self.send_message(
                    chat_id,
                    f"🎉 **تم إنشاء المشروع بنجاح على سطح المكتب!**\n\n"
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

        # Command: Run Safe Terminal Command (/cmd, /terminal, or natural install intent)
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
