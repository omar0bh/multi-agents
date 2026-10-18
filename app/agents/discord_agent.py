import json
import logging
from app.tools.discord_tool import (
    get_bot_guilds,
    get_guild_details,
    execute_server_blueprint,
)

logger = logging.getLogger(__name__)


# Default highly-aesthetic templates for instant reliability
DEFAULT_BLUEPRINTS = {
    "gaming": {
        "theme": "Gaming & Esports Community",
        "roles": [
            {"name": "👑 │ Server Owner", "color": 0xFFD700, "hoist": True},
            {"name": "🛡️ │ Administrator", "color": 0xE74C3C, "hoist": True},
            {"name": "👮 │ Moderator", "color": 0x3498DB, "hoist": True},
            {"name": "💎 │ VIP Gamer", "color": 0x9B59B6, "hoist": True},
            {"name": "🎮 │ Active Gamer", "color": 0x2ECC71, "hoist": True},
            {"name": "🤖 │ Bots", "color": 0x7289DA, "hoist": False},
            {"name": "👥 │ Community", "color": 0x95A5A6, "hoist": False},
        ],
        "categories": [
            {
                "name": "📌 │ INFORMATION",
                "channels": [
                    {"name": "📜・قوانين-rules", "type": "text", "topic": "قوانين السيرفر الرسمية والتوجيهات"},
                    {"name": "📢・إعلانات-announcements", "type": "text", "topic": "كل المستجدات وتحديثات السيرفر"},
                    {"name": "👋・ترحيب-welcome", "type": "text", "topic": "ترحيب بالأعضاء الجدد"},
                ],
            },
            {
                "name": "💬 │ CHAT & COMMUNITY",
                "channels": [
                    {"name": "💬・الدردشة-العامة", "type": "text", "topic": "مكان السوالف والدردشة العامة"},
                    {"name": "🎮・gaming-talk", "type": "text", "topic": "نقاشات الألعاب، الكليبات والإنجازات"},
                    {"name": "📷・صور-وفيديوهات", "type": "text", "topic": "شارك صورك وسيت أب الألعاب"},
                    {"name": "🤖・bot-commands", "type": "text", "topic": "أوامر البوتات والألعاب التفاعلية"},
                ],
            },
            {
                "name": "🔊 │ VOICE CHANNELS",
                "channels": [
                    {"name": "☕・مجلس-السوالف", "type": "voice"},
                    {"name": "🎮・Duo Room 1", "type": "voice"},
                    {"name": "🎮・Squad Room 1", "type": "voice"},
                    {"name": "🔊・Gaming Lounge", "type": "voice"},
                    {"name": "💤・AFK", "type": "voice"},
                ],
            },
            {
                "name": "🛡️ │ STAFF AREA",
                "channels": [
                    {"name": "🔒・staff-chat", "type": "text", "topic": "دردشة سرية خاصة بالإدارة"},
                    {"name": "🚨・mod-logs", "type": "text", "topic": "سجلات التحذير والباند والمراقبة"},
                    {"name": "🔒・Staff Meeting", "type": "voice"},
                ],
            },
        ],
        "rules_embed": {
            "title": "📜 قوانين السيرفر الرسمية (Server Rules)",
            "description": "مرحباً بك في مجتمعنا! لضمان بيئة لعب محترمة وممتعة للجميع، يرجى الالتزام بالقواعد التالية:",
            "color": 0x3498DB,
            "fields": [
                {"name": "1️⃣ الاحترام المتبادل", "value": "ممنوع السب، الشتم، التعدي اللفظي أو العنصرية بجميع أشكالها."},
                {"name": "2️⃣ الإعلانات والروابط (No Self-Promo)", "value": "ممنوع نشر روابط سيرفرات أخرى أو الترويج الشخصي بدون إذن الإدارة."},
                {"name": "3️⃣ استخدام القنوات المناسبة", "value": "يرجى الالتزام بموضوع كل قناة (الأوامر في bot-commands، الألعاب في gaming-talk)."},
                {"name": "4️⃣ منع السبام والإزعاج", "value": "تجنب التكرار المفرط للرسائل أو المنشن المتكرر للأعضاء والإدارة."},
                {"name": "5️⃣ سلامة المايكروفون في الرومات", "value": "يمنع تشغيل الأصوات المزعجة أو الصراخ في القنوات الصوتية."},
            ],
            "footer": {"text": "🛡️ إدارة السيرفر - نتمنى لكم أوقاتاً ممتعة!"},
        },
    },
    "coding": {
        "theme": "Software Developers & Tech Community",
        "roles": [
            {"name": "👑 │ Tech Lead", "color": 0xF1C40F, "hoist": True},
            {"name": "🛡️ │ Admin / Core Dev", "color": 0xE67E22, "hoist": True},
            {"name": "💻 │ Senior Engineer", "color": 0x3498DB, "hoist": True},
            {"name": "🚀 │ Developer", "color": 0x2ECC71, "hoist": True},
            {"name": "🤖 │ AI & Bots", "color": 0x9B59B6, "hoist": False},
            {"name": "🌱 │ Student / Junior", "color": 0x1ABC9C, "hoist": False},
        ],
        "categories": [
            {
                "name": "📌 │ TECH & RULES",
                "channels": [
                    {"name": "📜・rules-and-code-of-conduct", "type": "text", "topic": "قواعد المجتمع البرمجي"},
                    {"name": "📢・announcements", "type": "text", "topic": "الأخبار والتحديثات والمشاريع الجديدة"},
                    {"name": "💼・jobs-and-gigs", "type": "text", "topic": "فرص العمل والمشاريع الحرة (Freelance)"},
                ],
            },
            {
                "name": "💻 │ DEV LOUNGES",
                "channels": [
                    {"name": "💬・general-dev", "type": "text", "topic": "نقاشات برمجية وتقنية عامة"},
                    {"name": "🐍・python-and-ai", "type": "text", "topic": "بايثون، الذكاء الاصطناعي، تعلم الآلة"},
                    {"name": "🌐・web-and-mobile", "type": "text", "topic": "React, Next.js, Node, Flutter, Web apps"},
                    {"name": "🛠️・help-and-debugging", "type": "text", "topic": "اطرح أخطاء الكود ومشاكل الـ Bugs"},
                    {"name": "🚀・project-showcase", "type": "text", "topic": "استعرض مشاريعك وأكوادك واحصل على تقييم"},
                ],
            },
            {
                "name": "🔊 │ AUDIO & PAIR PROGRAMMING",
                "channels": [
                    {"name": "☕・Dev Coffee", "type": "voice"},
                    {"name": "💻・Pair Programming 1", "type": "voice"},
                    {"name": "💻・Pair Programming 2", "type": "voice"},
                    {"name": "🎧・Focus / Study Room", "type": "voice"},
                ],
            },
        ],
        "rules_embed": {
            "title": "💻 Code of Conduct & Guidelines",
            "description": "مرحباً بكم في مجتمع المطورين والمهندسين البرمجيين:",
            "color": 0x2ECC71,
            "fields": [
                {"name": "1️⃣ شارك الكود بتنسيق صحيح", "value": "استخدم Markdown Codeblocks (```python) عند نشر الأكواد."},
                {"name": "2️⃣ المساعدة البناءة", "value": "ساعد الآخرين باحترام وتجنب الاستهزاء بالمبتدئين."},
                {"name": "3️⃣ منع البرمجيات الخبيثة", "value": "يمنع منعاً باتاً نشر برمجيات خبيثة أو أكواد اختراق ضارة."},
            ],
            "footer": {"text": "🚀 Built with love by DSPy AI Architect"},
        },
    },
}


class DiscordArchitectAgent:
    def __init__(self):
        self.last_planned_blueprint: dict | None = None
        self.last_target_guild_id: str | None = None

    def plan(self, theme: str = "gaming", target_guild_id: str | None = None) -> dict:
        """Generate a complete server architecture blueprint tailored to the user's desired theme."""
        guilds = get_bot_guilds()
        if not guilds:
            return {
                "ok": False,
                "error": "الـ Bot غير موجود في أي سيرفر حالياً! تأكد من دعوته عبر رابط الدعوة بصلاحية Administrator.",
            }

        # If no guild provided, pick the first one
        selected_guild = None
        if target_guild_id:
            for g in guilds:
                if g["id"] == str(target_guild_id):
                    selected_guild = g
                    break
        if not selected_guild:
            selected_guild = guilds[0]

        guild_id = selected_guild["id"]
        guild_name = selected_guild["name"]

        # Select blueprint based on theme keywords
        theme_clean = theme.lower().strip()
        if any(k in theme_clean for k in ("code", "coding", "dev", "برمجة", "تطوير", "python", "web")):
            blueprint = DEFAULT_BLUEPRINTS["coding"]
        else:
            blueprint = DEFAULT_BLUEPRINTS["gaming"]

        self.last_planned_blueprint = blueprint
        self.last_target_guild_id = guild_id

        # Format Telegram Preview
        preview_text = self.format_preview(guild_name, blueprint)

        return {
            "ok": True,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "theme": blueprint["theme"],
            "blueprint": blueprint,
            "preview": preview_text,
        }

    def format_preview(self, guild_name: str, blueprint: dict) -> str:
        """Create a clear, structured preview for the user in Telegram."""
        lines = [
            f"🏛️ **تصميم وهندسة سيرفر ديسكورد (Discord Server Blueprint):**",
            f"🎯 **السيرفر المستهدف:** `{guild_name}`",
            f"🎨 **نوع المجتمع (Theme):** `{blueprint['theme']}`\n",
            f"👥 **الرتب المقترحة ({len(blueprint['roles'])} رتب):**",
        ]

        for r in blueprint["roles"]:
            lines.append(f"• `{r['name']}`")

        lines.append(f"\n📁 **الأقسام والقنوات المقترحة ({len(blueprint['categories'])} أقسام):**")
        for cat in blueprint["categories"]:
            lines.append(f"\n📂 **{cat['name']}**")
            for ch in cat["channels"]:
                icon = "🔊" if ch.get("type") == "voice" else "#️⃣"
                lines.append(f"  {icon} `{ch['name']}`")

        lines.append("\n────────────────────────")
        lines.append("⚠️ **نظام الحماية والموافقة (Human-in-the-loop):**")
        lines.append("• لم يتم تنفيذ أي تعديل على سيرفرك حتى الآن.")
        lines.append("• للموافقة وبدء بناء السيرفر تلقائياً، أرسل:\n👉 `/discord apply` أو قل: *'طبق تصميم الديسكورد'*")

        return "\n".join(lines)

    def apply_current_plan(self, guild_id: str | None = None) -> dict:
        """Execute the last approved plan on the target guild."""
        target_id = guild_id or self.last_target_guild_id
        if not target_id:
            # Check default guild
            guilds = get_bot_guilds()
            if guilds:
                target_id = guilds[0]["id"]
            else:
                return {"ok": False, "error": "لم يتم العثور على أي سيرفر لتطبيق التصميم."}

        blueprint = self.last_planned_blueprint
        if not blueprint:
            # Use gaming as default
            blueprint = DEFAULT_BLUEPRINTS["gaming"]

        # Execute
        results = execute_server_blueprint(target_id, blueprint)
        return {
            "ok": True,
            "guild_id": target_id,
            "results": results,
            "summary": (
                f"✅ **تم بناء وتنسيق سيرفر ديسكورد بنجاح!**\n\n"
                f"👑 **الرتب المنشأة:** {len(results['roles_created'])}\n"
                f"📂 **الأقسام المنشأة:** {len(results['categories_created'])}\n"
                f"💬 **القنوات المنشأة:** {len(results['channels_created'])}\n"
                f"📜 **القوانين والترحيب:** تم نشر رسالة القوانين الرسمية تلقائياً!"
            ),
        }
