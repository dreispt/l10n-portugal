# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
# Reference: https://github.com/bitmario/invoicexpress-api-python

import json
import logging
import pprint

import requests
from werkzeug.urls import url_join

from odoo import exceptions, models

_logger = logging.getLogger(__name__)


class InvoiceXpressAdapter(models.AbstractModel):
    _name = "invoice.api.adapter.invoicexpress"
    _inherit = "invoice.api.adapter"
    _description = "InvoiceXpress Adapter"

    def _get_backend(self):
        backend_id = self.env.context.get("invoice_api_backend_id")
        return self.env["invoice.api.backend"].browse(backend_id)

    def _get_config(self):
        backend = self._get_backend()
        account_name = backend.invoicexpress_account_name
        api_key = backend.api_key
        if not account_name or not api_key:
            raise exceptions.ValidationError(
                self.env._(
                    "InvoiceXpress backend %(backend)s is missing account name or API key.",
                    backend=backend.name,
                )
            )
        return {"account_name": account_name, "api_key": api_key}

    def _build_url(self, config, path):
        base_url = "https://{}.app.invoicexpress.com/".format(config["account_name"])
        return url_join(base_url, path)

    def _build_headers(self, config, headers_add=None):
        headers = {"content-type": "application/json", "accept": "application/json"}
        if headers_add:
            headers.update(headers_add)
        return headers

    def _build_params(self, config, params_add):
        params = {"api_key": config["api_key"]}
        if params_add:
            params.update(params_add)
        return params

    def _check_http_status(self, response):
        if response.status_code not in [200, 201]:
            msg = ""
            if response.text and response.text.startswith("{"):
                errors = response.json().get("errors", [])
                if isinstance(errors, list) and len(errors) > 0:
                    msg = "\n".join(
                        "- " + (x.get("error") or repr(x)) for x in errors
                    )
                elif isinstance(errors, dict):
                    msg = errors.get("error", False) or repr(errors)
            else:
                msg = repr(response.text)
            raise exceptions.ValidationError(
                self.env._(
                    "Error running API request (%(status_code)s %(reason)s):\n%(json)s",
                    status_code=response.status_code,
                    reason=response.reason,
                    json=msg,
                )
            )

    def _call(self, endpoint, verb="GET", headers=None, params=None, payload=None):
        config = self._get_config()
        request_url = self._build_url(config, endpoint)
        request_headers = self._build_headers(config, headers)
        request_params = self._build_params(config, params)
        request_data = payload and json.dumps(payload) or ""
        _logger.debug(
            "\nRequest for %s %s:\n%s",
            request_url,
            verb,
            pprint.pformat(payload, indent=1),
        )
        response = requests.request(
            verb,
            request_url,
            params=request_params,
            data=request_data,
            headers=request_headers,
            timeout=15,
        )
        _logger.debug(
            "\nResponse %s: %s",
            response.status_code,
            pprint.pformat(response.json(), indent=1)
            if response.text.startswith("{")
            else response.text,
        )
        self._check_http_status(response)
        return response

    def _format_date(self, date):
        if not date:
            return ""
        return date.strftime("%d/%m/%Y")

    def _get_prefix(self, doctype):
        return {
            "invoice": "FT",
            "invoice_receipt": "FR",
            "simplified_invoice": "FS",
            "vat_moss_invoice": "FVM",
            "credit_note": "NC",
            "debit_note": "ND",
        }.get(doctype)

    def _build_invoice_payload(self, invoice_vals):
        doctype = invoice_vals.get("document_type") or "invoice"
        items = []
        for line in invoice_vals.get("lines", []):
            items.append(
                {
                    "name": line.get("name"),
                    "description": line.get("description"),
                    "unit_price": line.get("unit_price"),
                    "quantity": line.get("quantity"),
                    "discount": line.get("discount"),
                    "tax": {
                        "name": line.get("tax", {}).get("name"),
                        "value": line.get("tax", {}).get("amount"),
                    },
                }
            )
        partner = invoice_vals.get("partner", {})
        client = {
            "name": partner.get("name"),
            "code": partner.get("external_ref"),
            "email": partner.get("email"),
            "address": partner.get("address"),
            "city": partner.get("city"),
            "postal_code": partner.get("zip_code"),
            "country": partner.get("country_name"),
            "fiscal_id": partner.get("vat"),
            "website": partner.get("website"),
            "phone": partner.get("phone"),
            "language": partner.get("language"),
        }
        payload = {
            doctype: {
                "date": self._format_date(invoice_vals.get("document_date")),
                "due_date": self._format_date(invoice_vals.get("due_date")),
                "reference": invoice_vals.get("reference"),
                "client": {k: v for k, v in client.items() if v},
                "observations": invoice_vals.get("notes"),
                "items": items,
            },
            "proprietary_uid": invoice_vals.get("proprietary_uid"),
        }
        if invoice_vals.get("tax_exemption"):
            payload[doctype]["tax_exemption"] = invoice_vals["tax_exemption"]
        if invoice_vals.get("exchange_rate"):
            payload[doctype]["currency_code"] = invoice_vals["currency"]
            payload[doctype]["rate"] = str(invoice_vals["exchange_rate"])
        if doctype in ("credit_note", "debit_note") and invoice_vals.get(
            "owner_invoice_id"
        ):
            payload[doctype]["owner_invoice_id"] = invoice_vals["owner_invoice_id"]
        return payload

    def create_invoice(self, invoice_vals):
        doctype = invoice_vals.get("document_type") or "invoice"
        payload = self._build_invoice_payload(invoice_vals)
        response = self._call(f"{doctype}s.json", "POST", payload=payload)
        values = response.json().get(doctype)
        if not values:
            raise exceptions.UserError(
                self.env._(
                    "Something went wrong: the InvoiceXpress response looks empty."
                )
            )
        external_id = values.get("id")
        permalink = values.get("permalink")
        response1 = self._call(
            f"{doctype}s/{external_id}/change-state.json",
            "PUT",
            payload={"invoice": {"state": "finalized"}},
        )
        values1 = response1.json().get(doctype)
        seqnum = values1 and values1.get("inverted_sequence_number")
        prefix = self._get_prefix(doctype)
        number = f"{prefix} {seqnum}" if prefix else seqnum
        return {
            "external_id": str(external_id),
            "number": number,
            "url": permalink,
            "state": "generated",
        }

    def cancel_invoice(self, external_id, reason=None):
        raise NotImplementedError(
            self.env._("InvoiceXpress cancellation is not implemented yet.")
        )

    def send_email(self, external_id, email_data):
        doctype = email_data.get("doctype")
        if not doctype:
            raise exceptions.UserError(
                self.env._("Missing InvoiceXpress document type for email.")
            )
        endpoint = f"{doctype}s/{external_id}/email-document.json"
        self._call(endpoint, "PUT", payload={"message": email_data["message"]})

    def mark_paid(self, external_id, doctype):
        response = self._call(
            f"{doctype}s/{external_id}/change-state.json",
            "PUT",
            payload={"invoice": {"state": "settled"}},
        )
        values = response.json().get(doctype)
        seqnum = values and values.get("inverted_sequence_number")
        if not seqnum:
            raise exceptions.UserError(
                self.env._(
                    "Something went wrong: the InvoiceXpress response"
                    " is missing a sequence number."
                )
            )

    def get_document_pdf(self, external_id):
        raise NotImplementedError(
            self.env._("PDF download is not implemented yet.")
        )
