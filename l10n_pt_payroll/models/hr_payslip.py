# Copyright (C) 2026 Daniel Reis
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import math

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import date_utils, float_round, format_amount, formatLang
from odoo.tools.safe_eval import safe_eval

# Non-residents are withheld at a flat 25% on Portuguese-source
# employment and pension income, without table deductions.
NON_RESIDENT_RATE = 25.0


class HrPayslip(models.Model):
    _inherit = "hr.payslip"

    payment_date = fields.Date(
        copy=False,
        help="Date the income is paid or made available. If blank, IRS uses the "
        "payslip period end.",
    )

    def copy(self, default=None):
        if default and default.get("credit_note"):
            default = {"payment_date": self.payment_date, **default}
        return super().copy(default)

    @api.constrains("state", "payment_date", "date_to")
    def _check_l10n_pt_irs_payment_order(self):
        starter = self.env.ref(
            "l10n_pt_payroll.hr_rule_pt_irs", raise_if_not_found=False
        )
        if not starter:
            return
        regular = self.filtered(
            lambda slip: slip.state == "done"
            and not slip.credit_note
            and starter in slip.line_ids.salary_rule_id
        )
        seen = set()
        for slip in regular:
            key = (
                slip.employee_id.id,
                slip.company_id.id,
                date_utils.start_of(slip._l10n_pt_irs_payment_date(), "month"),
            )
            if key in seen:
                raise ValidationError(
                    self.env._(
                        "Confirm IRS payslips for the same employee and month "
                        "separately."
                    )
                )
            seen.add(key)
        for slip in regular:
            slip._l10n_pt_irs_previous_lines()

    def _l10n_pt_ss_base(self, categories, rules, result_rules, current_contract):
        self.ensure_one()
        key = "l10n_pt_ss_base"
        if key in current_contract.dict:
            return current_contract.dict[key]

        base = categories.BASIC
        for code, rule in rules.dict.items():
            treatment = rule.l10n_pt_ss_contribution_base
            if not treatment:
                continue
            category = rule.category_id
            basic_count = 0
            while category:
                basic_count += category.code == "BASIC"
                category = category.parent_id
            base += (int(treatment == "include") - basic_count) * result_rules.dict[
                code
            ].total

        current_contract.dict[key] = base
        return base

    def _l10n_pt_ss_line_values(self, name, base, rate):
        result_name = self.env._(
            "%(name)s (%(rate)s%%)",
            name=name,
            rate=formatLang(self.env, rate, digits=4),
        )
        digits = self.env["decimal.precision"].precision_get("Payroll")
        rate_digits = self.env["decimal.precision"].precision_get("Payroll Rate")
        total = base * rate / 100.0
        displayed_total = (
            float_round(base, precision_digits=digits)
            * float_round(rate, precision_digits=rate_digits)
            / 100.0
        )
        if float_round(total, precision_digits=digits) != float_round(
            displayed_total, precision_digits=digits
        ):
            return (
                total,
                100.0,
                self.env._(
                    "%(name)s - Contribution base: %(base)s",
                    name=result_name,
                    base=format_amount(
                        self.env, abs(base), self.company_id.currency_id
                    ),
                ),
            )
        return base, rate, result_name

    # Monthly IRS retention (retencao na fonte) per the annual Despacho
    # tables. Called from the IRS salary rule's python compute with the
    # payslip's result_rules; returns the positive retention amount,
    # truncated down to the euro as the Despacho requires. The details
    # variant also returns the taxable base, applied rate and formula
    # deductions so the payslip line can present them in its name.
    def _l10n_pt_irs_retention(self, result_rules):
        return self._l10n_pt_irs_retention_details(result_rules)["amount"]

    def _l10n_pt_irs_line_name(self, name, details):
        currency = self.company_id.currency_id
        base = details["base"]
        taxable = details["taxable"]
        name = self.env._(
            "%(name)s - Taxable base: %(base)s",
            name=name,
            base=format_amount(self.env, base, currency),
        )
        if taxable > base and taxable > 0:
            name = self.env._(
                "%(name)s (income before relief: %(amount)s)",
                name=name,
                amount=format_amount(self.env, taxable, currency),
            )
        if details.get("allowance_applied"):
            name = self.env._(
                "%(name)s; single-payer allowance applied: %(allowance)s",
                name=name,
                allowance=format_amount(
                    self.env, details["allowance_applied"], currency
                ),
            )
        if details["rate"] is not None:
            name = self.env._(
                "%(name)s; applied rate: %(rate)s%%; formula deductions: %(deduction)s",
                name=name,
                rate=formatLang(self.env, details["rate"], digits=3),
                deduction=format_amount(self.env, details["parcela"], currency),
            )
        if details.get("has_previous"):
            name = self.env._(
                "%(name)s; month-to-date withholding: %(monthly)s; "
                "previously withheld: %(previous)s",
                name=name,
                monthly=format_amount(self.env, details["month_amount"], currency),
                previous=format_amount(
                    self.env, details["prior_withholding"], currency
                ),
            )
        if taxable > 0:
            name = self.env._(
                "%(name)s; monthly effective withholding rate: %(effective)s%%",
                name=name,
                effective=formatLang(
                    self.env, details["month_amount"] * 100.0 / taxable, digits=2
                ),
            )
        return name

    def _l10n_pt_irs_retention_details(self, result_rules):
        self.ensure_one()
        version = self.contract_id
        if not version:
            raise UserError(self.env._("The payslip has no contract."))
        current = self._l10n_pt_irs_current_taxable(result_rules, version)
        allowance = self._l10n_pt_irs_current_allowance(current, version)
        previous = self.env["hr.payslip.line"]
        if not self.credit_note:
            previous = self._l10n_pt_irs_previous_lines()
        prior_withholding = -sum(previous.mapped("total"))
        prior_taxable, prior_allowance = self._l10n_pt_irs_prior_amounts(previous)
        details = self._l10n_pt_irs_calculate(
            version,
            current + prior_taxable,
            allowance,
            prior_allowance,
        )
        month_amount = details["amount"]
        if (
            self.company_id.currency_id.compare_amounts(month_amount, prior_withholding)
            < 0
        ):
            raise UserError(
                self.env._(
                    "IRS already withheld exceeds the recalculated monthly amount. "
                    "Review the earlier payslips before continuing."
                )
            )
        details.update(
            amount=month_amount - prior_withholding,
            month_amount=month_amount,
            prior_withholding=prior_withholding,
            has_previous=bool(previous),
        )
        return details

    def _l10n_pt_irs_payment_date(self):
        self.ensure_one()
        return self.payment_date or self.date_to

    def _l10n_pt_irs_previous_lines(self):
        payment_date = self._l10n_pt_irs_payment_date()
        start = date_utils.start_of(payment_date, "month")
        end = date_utils.end_of(payment_date, "month")
        month = (
            Domain("slip_id.payment_date", ">=", start)
            & Domain("slip_id.payment_date", "<=", end)
        ) | (
            Domain("slip_id.payment_date", "=", False)
            & Domain("slip_id.date_to", ">=", start)
            & Domain("slip_id.date_to", "<=", end)
        )
        lines = self.env["hr.payslip.line"].search(
            month
            & Domain(
                [
                    ("slip_id", "!=", self.id),
                    ("slip_id.state", "=", "done"),
                    ("slip_id.employee_id", "=", self.employee_id.id),
                    ("slip_id.company_id", "=", self.company_id.id),
                    ("code", "=", "IRS"),
                ]
            )
        )
        if lines and (
            not self.payment_date
            or any(not slip.payment_date for slip in lines.slip_id)
        ):
            raise ValidationError(
                self.env._(
                    "Set a payment date on each IRS payslip when there is more than "
                    "one payment in the month."
                )
            )
        if any(
            line.slip_id.credit_note
            or line.slip_id._l10n_pt_irs_payment_date() > payment_date
            for line in lines
        ):
            raise UserError(
                self.env._(
                    "Review the monthly IRS manually before processing a backdated "
                    "payslip or a month with an IRS reversal."
                )
            )
        return lines

    def _l10n_pt_irs_prior_amounts(self, withholding_lines):
        taxable = allowance = 0.0
        if not withholding_lines:
            return taxable, allowance
        base_rule = self.env.ref("l10n_pt_payroll.hr_rule_pt_irs_base")
        allowance_rule = self.env.ref("l10n_pt_payroll.hr_rule_pt_irs_allowance")
        for line in withholding_lines:
            recorded = line.slip_id.line_ids.filtered(
                lambda item, contract=line.contract_id: item.contract_id == contract
            )
            bases = recorded.filtered(lambda item: item.salary_rule_id == base_rule)
            allowances = recorded.filtered(
                lambda item: item.salary_rule_id == allowance_rule
            )
            if len(bases) != 1 or len(allowances) != 1:
                raise UserError(
                    self.env._(
                        "An earlier payslip has no unique record of its IRS taxable "
                        "base and allowance. Review the monthly withholding before "
                        "processing another payslip."
                    )
                )
            taxable += bases.total
            allowance += allowances.total
        return taxable, allowance

    def _l10n_pt_irs_current_taxable(self, result_rules, version):
        structure = self.struct_id or version.struct_id
        taxable = 0.0
        for rule in structure.get_all_rules():
            if (
                rule.l10n_pt_irs_taxable
                and rule.code
                and rule.code != "IRS"
                and rule.code in result_rules.dict
            ):
                taxable += result_rules.dict[rule.code].total * rule.l10n_pt_irs_taxable
        return self._l10n_pt_irs_round_input(taxable)

    def _l10n_pt_irs_current_allowance(self, taxable, version):
        allowance = self._l10n_pt_irs_round_input(
            self._l10n_pt_irs_single_payer_amount(version)
        )
        return min(max(taxable, 0.0), allowance)

    def _l10n_pt_irs_round_input(self, amount):
        return float_round(
            amount,
            precision_digits=self.env["decimal.precision"].precision_get("Payroll"),
        )

    def _l10n_pt_irs_calculate(self, version, taxable, allowance, previous_allowance):
        if (allowance or previous_allowance) and (
            not version.l10n_pt_irs_non_resident
            or version.l10n_pt_irs_pensioner
            or not version.l10n_pt_irs_single_payer_declared
        ):
            raise UserError(
                self.env._(
                    "The single-payer allowance requires a non-resident worker "
                    "with a written single-payer declaration."
                )
            )
        if taxable <= 0:
            return dict(amount=0.0, taxable=taxable, base=0.0, rate=None, parcela=0.0)
        if version.l10n_pt_irs_non_resident:
            base = max(taxable - allowance - previous_allowance, 0.0)
            return dict(
                amount=float(math.floor(base * NON_RESIDENT_RATE / 100.0)),
                taxable=taxable,
                base=base,
                rate=NON_RESIDENT_RATE if base else None,
                parcela=0.0,
                allowance_applied=allowance,
            )
        table = self._l10n_pt_irs_withholding_table(
            version, self._l10n_pt_irs_payment_date()
        )
        base = taxable
        if version.l10n_pt_irs_jovem and not version.l10n_pt_irs_pensioner:
            base = self._l10n_pt_irs_jovem_base(taxable, table, version)
        if base <= 0:
            return dict(amount=0.0, taxable=taxable, base=base, rate=None, parcela=0.0)
        bracket = table.line_ids.filtered(
            lambda line: line.lower_bound < taxable <= (line.upper_bound or math.inf)
        )
        if len(bracket) != 1:
            raise UserError(
                self.env._(
                    "No unique IRS withholding bracket found for "
                    "remuneration %(amount)s on table %(table)s.",
                    amount=taxable,
                    table=table.display_name,
                )
            )
        bracket = bracket[0]
        # Despacho n.5-h: three or more dependents reduce the marginal
        # rate by 1 p.p. A preferred rate (n.5-e) replaces the marginal
        # rate but keeps every deduction unchanged.
        rate = bracket.marginal_rate - (
            1.0 if (version.l10n_pt_irs_dependents or 0) >= 3 else 0.0
        )
        if version.l10n_pt_irs_preferred_rate:
            rate = version.l10n_pt_irs_preferred_rate
        dependents = version.l10n_pt_irs_dependents or 0
        disabled_dependents = version.l10n_pt_irs_disabled_dependents or 0
        parcela = safe_eval(bracket.parcela_abater or "0", {"R": taxable})
        parcela += bracket.dependent_parcela * dependents
        # Despacho n.5-a + n.6: each dependent with a disability of 60%
        # or more adds the per-period amount by family situation,
        # optionally multiplied by the factor the employee declared.
        if disabled_dependents:
            parcela += (
                self._l10n_pt_disabled_dep_parcela(version, table)
                * disabled_dependents
                * (version.l10n_pt_irs_disabled_dep_factor or 1)
            )
        # Despacho n.5-b: married worker filing alone whose spouse has a
        # disability of 60% or more and no category A/H income.
        if (
            version.l10n_pt_irs_family_status == "married_single_income"
            and version.l10n_pt_irs_spouse_disabled
        ):
            parcela += table.spouse_disabled_parcela
        if version.l10n_pt_irs_pensioner:
            # Despacho n.5-c: pensioners with dependents get an extra
            # per-dependent amount that depends on the family situation.
            parcela += self._l10n_pt_pension_dep_parcela(version, table) * dependents
            # Despacho n.5-d / formula 3.c: armed forces disability adds
            # a flat amount on tables X and XI.
            if version.l10n_pt_military_disabled:
                parcela += bracket.forces_disabled_parcela
        retention = max(taxable * rate / 100.0 - parcela, 0.0)
        if base < taxable:
            # AT Circular 20274/2025: apply the full-income effective
            # rate to the non-exempt portion, not a second table bracket.
            rate = retention / taxable * 100.0
            retention = retention * base / taxable
            parcela = 0.0
        return dict(
            amount=float(math.floor(retention)),
            taxable=taxable,
            base=base,
            rate=rate,
            parcela=parcela,
        )

    def _l10n_pt_irs_single_payer_amount(self, version):
        allowance_inputs = self.input_line_ids.filtered(
            lambda line: line.code == "PT_IRS_SINGLE_PAYER"
            and line.contract_id == version
        )
        if len(allowance_inputs) > 1:
            raise UserError(
                self.env._("Enter only one single-payer allowance per contract.")
            )
        allowance = allowance_inputs.amount if allowance_inputs else 0.0
        if not math.isfinite(allowance) or allowance < 0:
            raise UserError(
                self.env._("The single-payer allowance must be non-negative.")
            )
        return allowance

    def _l10n_pt_irs_withholding_table(self, version, payment_date):
        self.ensure_one()
        table_variant = version.l10n_pt_irs_table_id
        if not table_variant:
            raise UserError(
                self.env._(
                    "Employee %(employee)s has no IRS withholding table "
                    "variant: set the IRS family status on the contract.",
                    employee=version.employee_id.name,
                )
            )
        # Overlapping sets are possible when a Despacho corrects part of
        # a period: the most recently effective set wins, and for equal
        # start dates the narrower (earlier-ending) window is preferred.
        table = self.env["hr.pt.irs.withholding.table"].search(
            [
                ("date_start", "<=", payment_date),
                "|",
                ("date_end", "=", False),
                ("date_end", ">=", payment_date),
                ("region", "=", version.l10n_pt_irs_region or "continente"),
                ("table_id", "=", table_variant.id),
            ],
            order="date_start desc, date_end asc, id desc",
            limit=1,
        )
        if not table:
            raise UserError(
                self.env._(
                    "No IRS withholding table covers %(date)s for region "
                    "%(region)s and table %(variant)s. Import the tables "
                    "for the period.",
                    date=payment_date,
                    region=version.l10n_pt_irs_region or "continente",
                    variant=table_variant.code,
                )
            )
        return table

    def _l10n_pt_disabled_dep_parcela(self, version, table):
        # Despacho n.5-a: single workers and married workers filing
        # alone get the higher amount; joint filers get half of it.
        self.ensure_one()
        if version.l10n_pt_irs_family_status == "married_joint":
            return table.disabled_dep_parcela_joint
        return table.disabled_dep_parcela_single

    def _l10n_pt_pension_dep_parcela(self, version, table):
        # Despacho n.5-c for pension tables VIII-XI.
        self.ensure_one()
        status = version.l10n_pt_irs_family_status
        if status == "married_single_income":
            return table.pension_dep_parcela_single_income
        if status == "married_joint":
            return table.pension_dep_parcela_joint
        return table.pension_dep_parcela_single

    # IRS Jovem (art. 12-B CIRS): the exemption applies to income up to
    # 55 x IAS per year; payroll withholds on the non-exempt portion.
    # Despacho n.5-g caps the accumulated monthly exemptions at the
    # annual limit divided by 14, approximated here as a monthly cap.
    def _l10n_pt_irs_jovem_base(self, taxable, table, version):
        # income_years counts the years of income already obtained, so
        # the current year is income_years + 1.
        income_year = (version.l10n_pt_irs_income_years or 0) + 1
        if income_year <= 1:
            exemption = 1.0
        elif income_year <= 4:
            exemption = 0.75
        elif income_year <= 7:
            exemption = 0.50
        elif income_year <= 10:
            exemption = 0.25
        else:
            exemption = 0.0
        monthly_cap = 55.0 * (table.ias or 0.0) / 14.0
        exempt = min(taxable * exemption, monthly_cap)
        return taxable - exempt
