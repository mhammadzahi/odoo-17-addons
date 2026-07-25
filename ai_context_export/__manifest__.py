{
    'name': 'AI Context Export',
    'version': '17.0.2.0.0',
    'summary': 'Generates Markdown context files describing installed apps, their config/settings, and data samples for AI-assisted development.',
    'description': """
AI Context Export
==================
Scans the whole Odoo instance — every installed app (core and custom), the
running server configuration (odoo.conf), system parameters
(ir.config_parameter), General Settings values per app, company records, and
a deep-dive per custom module of its models/fields/sample records — and
packages it all as Markdown files that can be handed to an external AI
assistant (Claude, ChatGPT, etc.) for fast, accurate context about this
specific Odoo project. Credential-looking values (passwords, secrets,
tokens, API keys) are redacted wherever they're found.

Usage:
- Go to Settings > Technical > AI Context Export
- Choose which instance-wide sections to include, and the deep-dive scope
- Click "Generate Context Files"
- Download the generated ZIP of .md files
""",
    'category': 'Technical',
    'author': 'Mohammed',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/ai_context_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
