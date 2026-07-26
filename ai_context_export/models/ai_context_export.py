# -*- coding: utf-8 -*-
import base64
import io
import logging
import zipfile
from datetime import datetime

from odoo import models, fields, api
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

# Modules that are considered "core/third-party" and typically don't need
# deep documentation (you can freely edit this list).
CORE_MODULE_PREFIXES = (
    'base', 'web', 'mail', 'portal', 'bus', 'http_routing', 'auth_',
)

# Substrings that mark a config/parameter/settings key as likely holding a
# credential or secret, so its value gets redacted instead of exported.
SENSITIVE_KEY_MARKERS = (
    'pass', 'secret', 'token', 'api_key', 'apikey', 'private_key',
    'access_key', 'auth_key', 'client_secret', 'credential',
)

# Field name prefixes/exact names that are generic framework "plumbing"
# (mail thread, activity, portal-access mixins, audit columns) present on
# almost every business model. They're deprioritized when picking which
# fields to sample, since showing them tells you nothing about a model's
# actual data shape.
GENERIC_FIELD_SKIP_PREFIXES = ('message_', 'activity_', 'access_')
GENERIC_FIELD_SKIP_NAMES = {
    'create_uid', 'create_date', 'write_uid', 'write_date',
    'display_name', '__last_update', 'website_message_ids',
}


def _is_sensitive(key):
    key_l = (key or '').lower()
    return any(marker in key_l for marker in SENSITIVE_KEY_MARKERS)


def _mask(value):
    return '***REDACTED***' if value else value


class AiContextExport(models.Model):
    _name = 'ai.context.export'
    _description = 'AI Context Export'
    _order = 'create_date desc'

    name = fields.Char(default='AI Context Export', required=True)
    create_date = fields.Datetime(string='Generated On', readonly=True)
    only_custom_modules = fields.Boolean(
        string='Deep-Dive Only Custom Modules',
        default=True,
        help="Controls the per-module model/field/sample deep dive only: if "
             "checked, skips Odoo's own core/base/web/mail/portal modules and "
             "only deep-dives your custom addons. The module overview, server "
             "config, system parameters, general settings, and company sections "
             "always cover the whole instance regardless of this option.",
    )
    sample_record_count = fields.Integer(
        string='Sample Records per Model',
        default=3,
        help="How many example records to pull per model, to show real data "
             "shape without dumping the whole database.",
    )
    include_server_config = fields.Boolean(
        string='Include Server Config', default=True,
        help="Dump the running odoo-bin process configuration (odoo.conf). "
             "Credential-looking values (db password, admin passwd, etc.) are "
             "redacted.",
    )
    include_system_parameters = fields.Boolean(
        string='Include System Parameters', default=True,
        help="Dump all ir.config_parameter key/value pairs (Settings > Technical "
             "> System Parameters). Values that look like passwords, secrets, "
             "tokens, or API keys are redacted.",
    )
    include_general_settings = fields.Boolean(
        string='Include General Settings', default=True,
        help="Dump the current value of every field on the Settings > General "
             "Settings screen (res.config.settings), grouped by the app(s) that "
             "define each field.",
    )
    include_company_info = fields.Boolean(
        string='Include Company Info', default=True,
        help="Dump res.company records (address, currency, fiscal localization, "
             "email/report layout settings, etc.).",
    )
    zip_file = fields.Binary(string='Context Files (ZIP)', readonly=True, attachment=False)
    zip_filename = fields.Char(string='Filename', readonly=True)
    state = fields.Selection(
        [('draft', 'Draft'), ('done', 'Done')], default='draft', readonly=True
    )
    log = fields.Text(string='Generation Log', readonly=True)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def action_generate(self):
        self.ensure_one()
        log_lines = []
        files = {}

        config_values = {
            'only_custom_modules': self.only_custom_modules,
            'sample_record_count': self.sample_record_count,
            'include_server_config': self.include_server_config,
            'include_system_parameters': self.include_system_parameters,
            'include_general_settings': self.include_general_settings,
            'include_company_info': self.include_company_info,
        }

        # Everything below runs on a separate, throwaway cursor/environment.
        # Some installed app's models/settings/rules can be in a broken state
        # (dangling fields, invalid ir.rule domains, orphaned views, ...) and a
        # query hitting that mid-generation aborts the *whole* Postgres
        # transaction - which would otherwise also take down the self.write()
        # call at the end that's supposed to save the result, even though that
        # write has nothing to do with the failure. Isolating the read-only
        # data-gathering on its own cursor means the worst case is one
        # section/module gets skipped and logged; the actual save can't be
        # collateral damage.
        new_cr = self.pool.cursor()
        try:
            worker_env = api.Environment(new_cr, self.env.uid, self.env.context)
            worker = worker_env[self._name].new(config_values)

            all_modules = worker._get_all_installed_modules()
            deep_dive_modules = worker._get_deep_dive_modules(all_modules)
            log_lines.append(
                f"{len(all_modules)} module(s) installed instance-wide; "
                f"{len(deep_dive_modules)} selected for deep-dive documentation."
            )

            files['00_OVERVIEW.md'] = worker._generate_overview_md(all_modules, deep_dive_modules)

            sections = [
                ('include_server_config', '01_SERVER_CONFIG.md',
                 worker._generate_server_config_md, 'server config'),
                ('include_system_parameters', '02_SYSTEM_PARAMETERS.md',
                 worker._generate_system_parameters_md, 'system parameters'),
                ('include_general_settings', '03_GENERAL_SETTINGS.md',
                 worker._generate_general_settings_md, 'general settings'),
                ('include_company_info', '04_COMPANIES.md',
                 worker._generate_companies_md, 'company info'),
            ]
            for flag, filename, generator, label in sections:
                if not config_values[flag]:
                    continue
                try:
                    files[filename] = generator()
                    log_lines.append(f"  - Documented {label}")
                except Exception as e:
                    log_lines.append(f"  - FAILED {label}: {e}")
                    _logger.exception("AI Context Export failed generating %s", label)
                    # A failed statement leaves the whole transaction "aborted"
                    # until it's rolled back - without this, every subsequent
                    # query on new_cr (including for later sections/modules)
                    # would fail too. This is a dedicated throwaway
                    # cursor/transaction, so a full rollback is safe here.
                    new_cr.rollback()
                    worker_env.clear()

            for module in deep_dive_modules:
                try:
                    content = worker._generate_module_md(module)
                    safe_name = module.name.replace('/', '_')
                    files[f'module_{safe_name}.md'] = content
                    log_lines.append(f"  - Documented module: {module.name}")
                except Exception as e:
                    log_lines.append(f"  - FAILED for module {module.name}: {e}")
                    _logger.exception("AI Context Export failed for module %s", module.name)
                    new_cr.rollback()
                    worker_env.clear()
        finally:
            new_cr.rollback()
            new_cr.close()

        # Package everything into a zip. This, and the write below, run on the
        # original/main cursor, which was never touched by any of the above.
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename, content in files.items():
                zf.writestr(filename, content)

        zip_data = zip_buffer.getvalue()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        self.write({
            'zip_file': base64.b64encode(zip_data),
            'zip_filename': f'odoo_ai_context_{timestamp}.zip',
            'state': 'done',
            'log': '\n'.join(log_lines),
        })
        return True

    # ------------------------------------------------------------------
    # Module discovery
    # ------------------------------------------------------------------
    def _get_all_installed_modules(self):
        return self.env['ir.module.module'].sudo().search(
            [('state', '=', 'installed')], order='name'
        )

    def _get_deep_dive_modules(self, all_modules):
        if self.only_custom_modules:
            return all_modules.filtered(
                lambda m: not m.name.startswith(CORE_MODULE_PREFIXES)
            )
        return all_modules

    # ------------------------------------------------------------------
    # Overview file
    # ------------------------------------------------------------------
    def _generate_overview_md(self, all_modules, deep_dive_modules):
        deep_dive_names = set(deep_dive_modules.mapped('name'))
        lines = [
            "# Odoo Instance — AI Context Overview",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Odoo version: {fields.first(all_modules).installed_version if all_modules else 'unknown'}",
            f"Total installed modules: {len(all_modules)}",
            "",
            "## All Installed Modules",
            "",
            "| Module | Category | Summary | Depends On | Deep-Dive File |",
            "|---|---|---|---|---|",
        ]
        for m in all_modules:
            deps = ', '.join(d.name for d in m.dependencies_id.mapped('depend_id')) or '-'
            summary = (m.summary or m.shortdesc or '').replace('\n', ' ').replace('|', '/')
            category = m.category_id.display_name if m.category_id else '-'
            deep_dive_file = f'module_{m.name}.md' if m.name in deep_dive_names else '-'
            lines.append(f"| {m.name} | {category} | {summary} | {deps} | {deep_dive_file} |")

        lines += [
            "",
            "## Other Files In This Archive",
            "",
            "- `01_SERVER_CONFIG.md` — odoo-bin process configuration (odoo.conf), credentials redacted",
            "- `02_SYSTEM_PARAMETERS.md` — ir.config_parameter key/values, sensitive values redacted",
            "- `03_GENERAL_SETTINGS.md` — Settings > General Settings current values, grouped by app",
            "- `04_COMPANIES.md` — res.company records (address, currency, fiscal/localization)",
            "- `module_<name>.md` — one file per deep-dived module: its own models, fields, and sample data",
            "",
            "## How to use this export",
            "",
            "Give this whole archive to an AI coding assistant as project context for "
            "this specific Odoo instance — every installed app, how it's configured, "
            "and real (sampled) data shape.",
        ]
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Server config (odoo.conf / odoo-bin options)
    # ------------------------------------------------------------------
    def _generate_server_config_md(self):
        options = dict(odoo_config.options)
        lines = [
            "# Server Configuration (odoo.conf / odoo-bin options)",
            "",
            "_Values that look like credentials or secrets are redacted._",
            "",
            "| Option | Value |",
            "|---|---|",
        ]
        for key in sorted(options):
            value = options[key]
            # Only redact string values - a boolean/int option merely whose
            # *name* contains a marker like "pass" (e.g. a hypothetical
            # `xmlrpc_passthrough` flag) can't actually hold a secret.
            if _is_sensitive(key) and isinstance(value, str):
                value = _mask(value)
            lines.append(f"| {key} | {value} |")
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # System parameters (ir.config_parameter)
    # ------------------------------------------------------------------
    def _generate_system_parameters_md(self):
        params = self.env['ir.config_parameter'].sudo().search([], order='key')
        lines = [
            "# System Parameters (ir.config_parameter)",
            "",
            "_Values that look like passwords, secrets, tokens, or API keys are redacted._",
            "",
            "| Key | Value |",
            "|---|---|",
        ]
        for p in params:
            value = (p.value or '').replace('\n', ' ')
            if _is_sensitive(p.key):
                value = _mask(value)
            lines.append(f"| {p.key} | {value} |")
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # General settings (res.config.settings)
    # ------------------------------------------------------------------
    def _generate_general_settings_md(self):
        Settings = self.env['res.config.settings'].sudo()
        skip = {'id', 'create_uid', 'create_date', 'write_uid', 'write_date', 'display_name'}
        field_names = [
            name for name, f in Settings._fields.items()
            if f.type != 'one2many' and name not in skip
        ]
        values = Settings.default_get(field_names)

        field_module = {}
        field_records = self.env['ir.model.fields'].sudo().search([
            ('model', '=', 'res.config.settings'),
        ])
        for f in field_records:
            if f.name not in field_names:
                continue
            group = f.modules or 'base'
            field_module.setdefault(group, set()).add(f.name)

        lines = [
            "# General Settings (res.config.settings current values)",
            "",
            "_Grouped by the module(s) that define each field — reflects what you'd "
            "see on Settings > General Settings for each installed app._",
            "",
        ]
        for group in sorted(field_module):
            lines.append(f"## {group}")
            lines.append("")
            lines.append("| Field | Value |")
            lines.append("|---|---|")
            for name in sorted(field_module[group]):
                value = values.get(name)
                if hasattr(value, 'display_name'):
                    value = value.display_name
                if isinstance(value, (list, tuple)):
                    value = ', '.join(str(v) for v in value)
                # By this point relational/list values have already been
                # stringified above, so anything still non-string here is a
                # boolean/int/date/etc. - those field types can't hold a real
                # secret even if the field name matches a sensitive marker
                # (e.g. the boolean `auth_signup_reset_password`).
                if _is_sensitive(name) and isinstance(value, str):
                    value = _mask(value)
                lines.append(f"| {name} | {value} |")
            lines.append("")
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Companies (res.company)
    # ------------------------------------------------------------------
    def _generate_companies_md(self):
        companies = self.env['res.company'].sudo().search([])
        info_fields = [
            'name', 'email', 'phone', 'website', 'vat', 'street', 'city',
            'country_id', 'state_id', 'currency_id', 'parent_id',
        ]
        lines = ["# Companies (res.company)", ""]
        for c in companies:
            lines.append(f"## {c.name} (ID {c.id})")
            for fname in info_fields:
                if fname not in c._fields:
                    continue
                val = getattr(c, fname)
                if hasattr(val, 'display_name'):
                    val = val.display_name
                lines.append(f"- {fname}: {val}")
            lines.append("")
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Per-module file
    # ------------------------------------------------------------------
    def _generate_module_md(self, module):
        lines = [
            f"# Module: {module.name}",
            "",
            f"**Summary:** {module.summary or module.shortdesc or '-'}",
            f"**Version:** {module.installed_version or '-'}",
            f"**Author:** {module.author or '-'}",
            "",
        ]

        models_in_module = self._get_models_for_module(module)

        if not models_in_module:
            lines.append("_No custom models detected for this module "
                         "(it may only add views, data, or extend other modules' fields)._")
            return '\n'.join(lines)

        failed_models = []
        for model_name in models_in_module:
            try:
                lines += self._generate_model_section(model_name)
            except Exception as e:
                failed_models.append(model_name)
                lines.append(f"## Model: `{model_name}`")
                lines.append("")
                lines.append(f"_Could not document this model: {e}_")
                lines.append("")
                # Throwaway cursor for this whole export - a full rollback is
                # the safe, reliable way to clear an aborted transaction so
                # the remaining models/modules can still be processed.
                self.env.cr.rollback()
                self.env.clear()
                _logger.exception("AI Context Export failed for model %s", model_name)

        if failed_models:
            lines.append(f"_{len(failed_models)} of {len(models_in_module)} model(s) "
                         f"could not be documented: {', '.join(failed_models)}_")
            lines.append("")

        return '\n'.join(lines)

    def _get_models_for_module(self, module):
        """Find models whose ir.model.data (xml_id) belongs to this module."""
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', module.name),
            ('model', '=', 'ir.model'),
        ])
        model_ids = imd.mapped('res_id')
        ir_models = self.env['ir.model'].sudo().browse(model_ids).exists()
        # Keep only models that actually exist in the registry right now
        return sorted(set(
            m.model for m in ir_models if m.model in self.env
        ))

    def _generate_model_section(self, model_name):
        Model = self.env[model_name].sudo()
        ir_model = self.env['ir.model'].sudo().search([('model', '=', model_name)], limit=1)

        lines = [
            f"## Model: `{model_name}`",
            "",
            f"**Description:** {ir_model.name or '-'}",
            "",
            "### Fields",
            "",
            "| Field | Type | Relation | Required | Stored/Computed |",
            "|---|---|---|---|---|",
        ]

        field_records = self.env['ir.model.fields'].sudo().search([
            ('model', '=', model_name),
        ])
        for f in field_records:
            relation = f.relation or '-'
            required = 'Yes' if f.required else 'No'
            stored = 'Computed' if f.compute else ('Stored' if f.store else 'Non-stored')
            lines.append(f"| {f.name} | {f.ttype} | {relation} | {required} | {stored} |")

        # Sample records
        lines += ["", "### Sample Records", ""]
        try:
            count = self.sample_record_count or 3
            records = Model.search([], limit=count)
            if not records:
                lines.append("_No records found in this model._")
            else:
                # Prefer the model's own business fields over generic
                # mail/activity/access-mixin plumbing that's present on
                # almost every model - otherwise the sample for a
                # mail-thread-enabled model is just 8 rows of `active`/
                # `activity_*`/`message_*`, never its actual data.
                candidate_fields = [
                    f for f in field_records
                    if f.name != 'id'
                    and f.name not in GENERIC_FIELD_SKIP_NAMES
                    and not f.name.startswith(GENERIC_FIELD_SKIP_PREFIXES)
                ] or [f for f in field_records if f.name != 'id']
                display_fields = [f.name for f in candidate_fields][:8]
                for rec in records:
                    lines.append(f"- **ID {rec.id}**")
                    for fname in display_fields:
                        try:
                            val = getattr(rec, fname)
                        except Exception:
                            continue
                        if hasattr(val, 'display_name'):
                            val = val.display_name
                        # Only redact once we know the actual value is a
                        # string - a boolean/int field merely named like a
                        # secret (e.g. `allow_tokenization`) isn't one.
                        if _is_sensitive(fname) and isinstance(val, str):
                            val = _mask(val)
                        lines.append(f"  - {fname}: {val}")
        except Exception as e:
            lines.append(f"_Could not fetch sample records: {e}_")

        lines.append("")
        return lines
