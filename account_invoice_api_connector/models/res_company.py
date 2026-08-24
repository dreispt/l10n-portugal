# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    invoice_api_template_id = fields.Many2one(
        "mail.template",
        "Invoice API Email Template",
        domain="[('model', '=', 'account.move')]",
        default=lambda self: self.env.ref(
            "account_invoice_api_connector.email_template_invoice", False
        ),
        help="Used to generate the To, Cc, Subject and Body"
        " for the email sent by the external invoice API service",
    )
