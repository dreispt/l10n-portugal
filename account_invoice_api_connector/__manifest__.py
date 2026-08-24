# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Account Invoice API Connector",
    "summary": "Generic base for external e-invoicing API integrations",
    "version": "19.0.1.0.0",
    "author": "Open Source Integrators, Odoo Community Association (OCA)",
    "license": "AGPL-3",
    "website": "https://github.com/OCA/l10n-portugal",
    "category": "Accounting/Accounting",
    "maintainers": ["dreispt"],
    "development_status": "Beta",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "security/ir_rules.xml",
        "views/res_config_settings.xml",
        "views/account_journal_view.xml",
        "views/account_move_view.xml",
        "data/mail_template.xml",
    ],
    "installable": True,
    "auto_install": False,
}
