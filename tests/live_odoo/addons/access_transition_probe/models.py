from odoo import fields, models


class AccessTransitionProbe(models.Model):
    _name = "access.transition.probe"
    _description = "Synthetic native access transition oracle"

    name = fields.Char(required=True)
    user_id = fields.Many2one("res.users", required=True)
    company_id = fields.Many2one("res.company", required=True)
    marker = fields.Char()


class AccessTransitionChild(models.Model):
    _name = "access.transition.child"
    _description = "Synthetic delegated access transition oracle"
    _inherits = {"access.transition.probe": "parent_id"}

    parent_id = fields.Many2one("access.transition.probe", required=True, ondelete="cascade")
