# -*- coding: utf-8 -*-

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    olive_face_model_profile_id = fields.Many2one(
        related="company_id.olive_face_model_profile_id", readonly=False)

    olive_face_match_threshold = fields.Float(
        related="company_id.olive_face_match_threshold", readonly=False)
    olive_face_review_threshold = fields.Float(
        related="company_id.olive_face_review_threshold", readonly=False)
    olive_face_margin_min = fields.Float(
        related="company_id.olive_face_margin_min", readonly=False)
    olive_face_frames_required = fields.Integer(
        related="company_id.olive_face_frames_required", readonly=False)
    olive_face_frame_window_ms = fields.Integer(
        related="company_id.olive_face_frame_window_ms", readonly=False)
    olive_face_cooldown_seconds = fields.Integer(
        related="company_id.olive_face_cooldown_seconds", readonly=False)

    olive_face_detector_input_size = fields.Integer(
        related="company_id.olive_face_detector_input_size", readonly=False)
    olive_face_min_face_px = fields.Integer(
        related="company_id.olive_face_min_face_px", readonly=False)
    olive_face_min_templates = fields.Integer(
        related="company_id.olive_face_min_templates", readonly=False)
    olive_face_liveness_threshold = fields.Float(
        related="company_id.olive_face_liveness_threshold", readonly=False)
    olive_face_liveness_required = fields.Boolean(
        related="company_id.olive_face_liveness_required", readonly=False)
    olive_face_ambiguous_size_ratio = fields.Float(
        related="company_id.olive_face_ambiguous_size_ratio", readonly=False)

    olive_face_max_clock_drift_seconds = fields.Integer(
        related="company_id.olive_face_max_clock_drift_seconds", readonly=False)
    olive_face_reject_clock_drift_seconds = fields.Integer(
        related="company_id.olive_face_reject_clock_drift_seconds", readonly=False)

    olive_face_toggle_gap_seconds = fields.Integer(
        related="company_id.olive_face_toggle_gap_seconds", readonly=False)
    olive_face_min_session_minutes = fields.Integer(
        related="company_id.olive_face_min_session_minutes", readonly=False)
    olive_face_presence_first = fields.Boolean(
        related="company_id.olive_face_presence_first", readonly=False)
    olive_face_expected_min_hours = fields.Float(
        related="company_id.olive_face_expected_min_hours", readonly=False)
    olive_face_expected_max_hours = fields.Float(
        related="company_id.olive_face_expected_max_hours", readonly=False)
    olive_face_pairing_mode = fields.Selection(
        related="company_id.olive_face_pairing_mode", readonly=False)
    olive_face_max_shift_hours = fields.Float(
        related="company_id.olive_face_max_shift_hours", readonly=False)
    olive_face_day_cutoff_hour = fields.Float(
        related="company_id.olive_face_day_cutoff_hour", readonly=False)
    olive_face_protect_validated = fields.Boolean(
        related="company_id.olive_face_protect_validated", readonly=False)
    olive_absence_min_confidence = fields.Float(
        related="company_id.olive_absence_min_confidence", readonly=False)
    olive_face_fold_inline = fields.Boolean(
        related="company_id.olive_face_fold_inline", readonly=False)

    olive_face_store_snapshot = fields.Selection(
        related="company_id.olive_face_store_snapshot", readonly=False)
    olive_face_snapshot_retention_days = fields.Integer(
        related="company_id.olive_face_snapshot_retention_days", readonly=False)
