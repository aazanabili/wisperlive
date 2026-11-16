"""Dialog state-machine tests for settings_view.py (plan 2.2).

Owner: qa-test-assurance.  Drives the real Tk dialog with a WITHDRAWN root
(exactly the worker's smoke pattern): provider change reprefill/reset,
readonly preset URL vs editable Custom, split rows visible only for MiMo,
token-plan variant -> cluster URL resolution, the R3 IFF save mapping
(split writes both model rows; single/Custom ALWAYS write ``transcription_model=""
``), and Escape/Cancel discarding without a save.

DISPLAY GATE: Tk requires a display.  The ubuntu CI leg (headless) skips this
class with a documented reason, matching the repo's env-gated-skip policy;
the windows leg runs it for real.  ``settings_view.save_config`` is always
patched - these tests never write a config file.
"""

import queue
import unittest
from unittest import mock

import config_manager
import providers
import settings_view

try:
    import tkinter as tk
except ImportError:  # a Python build without Tk cannot assert dialog behaviour
    tk = None

import main  # noqa: E402  (import after the Tk guard so the skip precedes it)


DEFAULT_CONFIG = {
    "api_key": "",
    "provider": "custom",
    "provider_variant": "",
    "provider_base_url": "",
    "model": "",
    "transcription_model": "",
    "target_language": "English",
    "theme": "dark",
}


class SettingsDialogTestCase(unittest.TestCase):
    """Shared withdrawn-root harness: fresh app + dialog per test."""

    @classmethod
    def setUpClass(cls):
        if tk is None:
            raise unittest.SkipTest("tkinter is not installed in this Python build")
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:  # headless CI leg (no $DISPLAY)
            raise unittest.SkipTest(f"no display available for Tk: {error}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.save_patch = mock.patch.object(settings_view, "save_config",
                                            return_value=True)
        self.save_mock = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
        self.app.root = self.root
        self.app.config = dict(DEFAULT_CONFIG)
        self.app.is_exiting = False
        self.app.is_processing = False
        self.app._ui_queue = queue.Queue()
        self.app.set_status = mock.MagicMock()
        self.app.show_settings()
        # Pump once while the dialog still exists: show_settings schedules
        # after_idle(provider_combo.focus_set); destroying the dialog before
        # that idle fires would emit a Tcl "invalid command" background error.
        self.root.update()
        self.w = self.app.settings_widgets

    def tearDown(self):
        dialog = getattr(self.app, "settings_widgets", {}).get("dialog")
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass
        self.app.settings_widgets = {}

    # --- helpers -----------------------------------------------------------
    def select_provider(self, label):
        self.w["provider_var"].set(label)
        self.w["provider_combo"].event_generate("<<ComboboxSelected>>")
        self.root.update()

    def select_variant(self, label):
        self.w["variant_var"].set(label)
        self.w["variant_combo"].event_generate("<<ComboboxSelected>>")
        self.root.update()

    @staticmethod
    def packed(widget):
        """True when the row is under the packer (independent of mapping)."""
        return widget.winfo_manager() == "pack"

    def reopen(self, config):
        """Replace the dialog with a fresh one hydrated from ``config``.

        Destroys the previous dialog (no leak, no stale grab) and pumps the
        idle queue while the new dialog exists so show_settings'
        after_idle(focus_set) cannot fire against a destroyed widget.
        """
        previous = getattr(self.app, "settings_widgets", {}).get("dialog")
        if previous is not None:
            try:
                previous.destroy()
            except tk.TclError:
                pass
        self.app.config = dict(config)
        self.app.settings_widgets = {}
        self.app.show_settings()
        self.root.update()
        self.w = self.app.settings_widgets
        return self.w


class HydrationTests(SettingsDialogTestCase):
    def test_provider_combo_offers_nine_approved_labels(self):
        self.assertEqual(tuple(self.w["provider_combo"].cget("values")),
                         providers.display_names())
        self.assertEqual(len(providers.display_names()), 9)
        # ttk returns an index object for the state option; str() unwraps it.
        self.assertEqual(str(self.w["provider_combo"].cget("state")), "readonly")

    def test_legacy_config_hydrates_as_custom_with_stored_url(self):
        # No inference: a pre-preset config shows Custom + byte-identical URL.
        w = self.reopen(dict(DEFAULT_CONFIG,
                             provider_base_url="https://legacy.example/v1",
                             model="old-model", api_key="legacy-key"))
        self.assertEqual(w["provider_var"].get(), "Custom (OpenAI-compatible URL)")
        self.assertEqual(w["base_var"].get(), "https://legacy.example/v1")
        self.assertEqual(w["model_var"].get(), "old-model")
        self.assertEqual(w["key_var"].get(), "legacy-key")
        # Custom: editable URL, single model row, no variant/split rows/hints.
        self.assertEqual(w["base_entry"].cget("state"), "normal")
        self.assertFalse(self.packed(w["variant_row"]))
        self.assertFalse(self.packed(w["mimo_rows"]))
        self.assertTrue(self.packed(w["model_row"]))
        self.assertFalse(self.packed(w["base_hint"]))

    def test_mimo_hydration_shows_stored_variant_and_curated_defaults(self):
        w = self.reopen(dict(
            DEFAULT_CONFIG, provider="xiaomi-mimo",
            provider_variant="token-plan-sgp",
            provider_base_url=providers.resolve_base_url("xiaomi-mimo",
                                                          "token-plan-sgp"),
            model="mimo-v2.6-pro", transcription_model="mimo-v2.5-asr"))
        self.assertEqual(w["provider_var"].get(), "Xiaomi MiMo")
        self.assertEqual(w["variant_var"].get(), "Token Plan \u2014 Singapore (SGP)")
        self.assertEqual(w["base_var"].get(),
                         "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(w["transcription_var"].get(), "mimo-v2.5-asr")
        self.assertEqual(w["model_var"].get(), "mimo-v2.6-pro")
        self.assertEqual(w["base_entry"].cget("state"), "readonly")
        self.assertTrue(self.packed(w["variant_row"]))
        self.assertTrue(self.packed(w["mimo_rows"]))
        self.assertFalse(self.packed(w["model_row"]))


class ProviderChangeTests(SettingsDialogTestCase):
    def test_selecting_mimo_shows_split_rows_and_readonly_url(self):
        self.select_provider("Xiaomi MiMo")
        self.assertTrue(self.packed(self.w["variant_row"]))
        self.assertTrue(self.packed(self.w["mimo_rows"]))
        self.assertFalse(self.packed(self.w["model_row"]))
        self.assertTrue(self.packed(self.w["base_hint"]))
        self.assertEqual(self.w["base_entry"].cget("state"), "readonly")
        self.assertEqual(self.w["base_var"].get(),
                         "https://api.xiaomimimo.com/v1")
        # Model fields reset to registry defaults (never another vendor's model)
        self.assertEqual(self.w["transcription_var"].get(), "mimo-v2.5-asr")
        self.assertEqual(self.w["model_var"].get(), "mimo-v2.6-flash")
        # Curated dropdown values come from the registry, no Fetch button.
        self.assertEqual(tuple(self.w["transcription_combo"].cget("values")),
                         ("mimo-v2.5-asr",))
        self.assertEqual(tuple(self.w["post_combo"].cget("values")),
                         ("mimo-v2.6-flash", "mimo-v2.6-pro"))

    def test_selecting_preset_resolves_registry_url_readonly(self):
        self.select_provider("Groq")
        self.assertEqual(self.w["base_var"].get(), "https://api.groq.com/openai/v1")
        self.assertEqual(self.w["base_entry"].cget("state"), "readonly")
        self.assertTrue(self.packed(self.w["model_row"]))
        self.assertFalse(self.packed(self.w["mimo_rows"]))
        self.assertFalse(self.packed(self.w["variant_row"]))
        self.assertEqual(self.w["model_var"].get(), "")  # no curated default
        self.assertEqual(self.w["transcription_var"].get(), "")

    def test_selecting_custom_keeps_current_url_and_editable_entry(self):
        self.select_provider("Xiaomi MiMo")
        self.select_variant("Token Plan \u2014 Singapore (SGP)")
        self.assertEqual(self.w["base_var"].get(),
                         "https://token-plan-sgp.xiaomimimo.com/v1")
        self.select_provider("Custom (OpenAI-compatible URL)")
        # Non-destructive: Custom inherits the currently effective URL.
        self.assertEqual(self.w["base_var"].get(),
                         "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(self.w["base_entry"].cget("state"), "normal")
        self.assertFalse(self.packed(self.w["variant_row"]))
        self.assertFalse(self.packed(self.w["mimo_rows"]))
        self.assertTrue(self.packed(self.w["model_row"]))

    def test_provider_change_resets_stored_variant_to_default(self):
        self.reopen(dict(
            DEFAULT_CONFIG, provider="xiaomi-mimo",
            provider_variant="token-plan-sgp",
            provider_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            model="mimo-v2.6-pro", transcription_model="mimo-v2.5-asr"))
        self.assertEqual(self.w["variant_var"].get(),
                         "Token Plan \u2014 Singapore (SGP)")
        # stored variant applies at open ONLY; provider changes reset to default
        self.select_provider("OpenAI")
        self.select_provider("Xiaomi MiMo")
        self.assertEqual(self.w["variant_var"].get(), "API (Pay-as-you-go)")
        self.assertEqual(self.w["base_var"].get(), "https://api.xiaomimimo.com/v1")
        self.assertEqual(self.w["model_var"].get(), "mimo-v2.6-flash")
        self.assertEqual(self.w["transcription_var"].get(), "mimo-v2.5-asr")

    def test_api_key_rehydrates_per_vendor_on_provider_switch_k7(self):
        """K7 BY DESIGN (plan R-U7; A3 K7; A5A7 474) — REPLACES the pre-K7
        ``test_api_key_survives_provider_switches`` (whose carry-across
        behaviour K7 changes by design, exactly like models never carry):

        on provider change the dialog must (a) file the typed key under the
        ORIGIN vendor slot and (b) re-hydrate ``key_var`` from the NEW
        vendor's slot (possibly ``""``).

        *** FAILING PIN (QA-2026-11-16): production defect reported to
        architect-orchestrator — ``settings_provider_view``'s
        ``apply_provider_change`` never calls
        ``config_manager.set_vendor_key(origin)/get_vendor_key(new)``, so the
        typed key carries across providers (misfiling risk, K7's exact stated
        hazard).  NOT fixed by QA (test-only writer); worker-executor owns the
        fix. ***
        """
        # seed a stored Groq key so rehydration from a NON-empty slot is provable
        self.app.config["api_keys_protected"] = {
            "groq": config_manager._protect_api_key("gsk-stored")}
        self.w["key_var"].set("sk-precious")            # typed for origin (custom)
        self.select_provider("Xiaomi MiMo")             # origin = custom
        # (b) field rehydrates from the NEW vendor's slot: never the old key
        self.assertEqual(self.w["key_var"].get(), "")
        # (a) the typed key was filed under the ORIGIN vendor
        self.assertEqual(config_manager.get_vendor_key(self.app.config, "custom"),
                         "sk-precious")
        # a vendor WITH a stored key re-hydrates that exact key
        self.select_provider("Groq")
        self.assertEqual(self.w["key_var"].get(), "gsk-stored")
        # switching BACK restores the origin vendor's own key (not the Groq one)
        self.select_provider("Custom (OpenAI-compatible URL)")
        self.assertEqual(self.w["key_var"].get(), "sk-precious")
        # no vendor slot ever leaks another vendor's key (K7 misattribution)
        self.assertEqual(
            config_manager.get_vendor_key(self.app.config, "xiaomi-mimo"), "")

    def test_all_split_rows_toggle_per_provider(self):
        split = "Xiaomi MiMo"
        others = ("OpenAI", "Google Gemini", "DeepSeek", "Groq", "Mistral",
                  "OpenRouter", "Ollama (local)", "Custom (OpenAI-compatible URL)")
        for label in others:
            with self.subTest(provider=label):
                self.select_provider(label)
                self.assertTrue(self.packed(self.w["model_row"]),
                                f"{label} must show the single Model row")
                self.assertFalse(self.packed(self.w["mimo_rows"]),
                                 f"{label} must hide the split rows")
                self.assertFalse(self.packed(self.w["variant_row"]),
                                 f"{label} must hide the variant row")
        self.select_provider(split)
        self.assertTrue(self.packed(self.w["mimo_rows"]))
        self.assertFalse(self.packed(self.w["model_row"]))
        self.assertTrue(self.packed(self.w["variant_row"]))


class VariantResolutionTests(SettingsDialogTestCase):
    def test_each_token_plan_variant_resolves_its_cluster_url(self):
        self.select_provider("Xiaomi MiMo")
        expected = {
            "API (Pay-as-you-go)": "https://api.xiaomimimo.com/v1",
            "Token Plan \u2014 Europe (AMS)":
                "https://token-plan-ams.xiaomimimo.com/v1",
            "Token Plan \u2014 Singapore (SGP)":
                "https://token-plan-sgp.xiaomimimo.com/v1",
            "Token Plan \u2014 China (CN)":
                "https://token-plan-cn.xiaomimimo.com/v1",
        }
        self.assertEqual(tuple(self.w["variant_combo"].cget("values")),
                         tuple(expected))
        for label, url in expected.items():
            with self.subTest(variant=label):
                self.select_variant(label)
                self.assertEqual(self.w["base_var"].get(), url)
                self.assertEqual(self.w["base_entry"].cget("state"), "readonly")

    def test_save_persists_selected_cluster_variant(self):
        self.select_provider("Xiaomi MiMo")
        self.select_variant("Token Plan \u2014 Singapore (SGP)")
        self.w["key_var"].set("tp-test")
        self.w["save_btn"].invoke()
        self.root.update()
        self.assertEqual(self.app.config["provider"], "xiaomi-mimo")
        self.assertEqual(self.app.config["provider_variant"], "token-plan-sgp")
        self.assertEqual(self.app.config["provider_base_url"],
                         "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(self.app.config["api_key"], "tp-test")


class SaveMappingTests(SettingsDialogTestCase):
    def test_split_save_writes_both_rows_and_registry_endpoint(self):
        self.select_provider("Xiaomi MiMo")
        self.select_variant("Token Plan \u2014 Europe (AMS)")
        self.w["transcription_var"].set("mimo-v2.5-asr")
        self.w["model_var"].set("mimo-v2.6-pro")
        self.w["save_btn"].invoke()
        self.root.update()
        config = self.app.config
        self.assertEqual(config["provider"], "xiaomi-mimo")
        self.assertEqual(config["provider_variant"], "token-plan-ams")
        self.assertEqual(config["provider_base_url"],
                         "https://token-plan-ams.xiaomimimo.com/v1")
        self.assertEqual(config["transcription_model"], "mimo-v2.5-asr")
        self.assertEqual(config["model"], "mimo-v2.6-pro")  # post row -> model
        self.assertNotIn("post_process_model", config)

    def test_single_save_always_clears_transcription_model(self):
        self.select_provider("Groq")
        self.w["model_var"].set("llama-3.3-70b-versatile")
        # Adversarial: a stale MiMo stage-1 value in the widget (missed reset)
        # must NEVER leak into a single-mode save (R3 IFF at the write path).
        self.w["transcription_var"].set("mimo-v2.5-asr")
        self.w["save_btn"].invoke()
        self.root.update()
        self.assertEqual(self.app.config["provider"], "groq")
        self.assertEqual(self.app.config["provider_variant"], "")
        self.assertEqual(self.app.config["provider_base_url"],
                         "https://api.groq.com/openai/v1")
        self.assertEqual(self.app.config["model"], "llama-3.3-70b-versatile")
        self.assertEqual(self.app.config["transcription_model"], "")

    def test_custom_save_uses_entered_url_and_clears_stage1(self):
        self.select_provider("Custom (OpenAI-compatible URL)")
        self.w["base_var"].set("  http://localhost:9000/v1  ")
        self.w["model_var"].set("my-model")
        self.w["transcription_var"].set("mimo-v2.5-asr")
        self.w["save_btn"].invoke()
        self.root.update()
        self.assertEqual(self.app.config["provider"], "custom")
        self.assertEqual(self.app.config["provider_variant"], "")
        self.assertEqual(self.app.config["provider_base_url"],
                         "http://localhost:9000/v1")  # trimmed, no re-resolution
        self.assertEqual(self.app.config["model"], "my-model")
        self.assertEqual(self.app.config["transcription_model"], "")

    def test_preset_save_re_resolves_url_ignoring_stale_display(self):
        # A tampered/stale URL in the readonly field cannot reach the config:
        # presets always persist the registry endpoint (M7 at the write path).
        self.select_provider("OpenAI")
        self.w["base_var"].set("https://attacker.example/v1")
        self.w["model_var"].set("gpt-x")
        self.w["save_btn"].invoke()
        self.root.update()
        self.assertEqual(self.app.config["provider_base_url"],
                         "https://api.openai.com/v1")

    def test_save_invokes_save_config_once_with_exact_status_copy(self):
        self.select_provider("OpenAI")
        self.w["model_var"].set("gpt-x")
        self.w["save_btn"].invoke()
        self.root.update()
        self.save_mock.assert_called_once()
        self.assertIs(self.save_mock.call_args.args[0], self.app.config)
        self.app.set_status.assert_called_once_with(
            "Provider settings saved. They apply to the next recording.", "ready")
        # dialog closed and scratch cleared
        self.assertEqual(self.app.settings_widgets, {})

    def test_save_failure_reports_error_status(self):
        self.save_mock.return_value = False
        self.select_provider("OpenAI")
        self.w["model_var"].set("gpt-x")
        self.w["save_btn"].invoke()
        self.root.update()
        self.app.set_status.assert_called_once_with(
            "Provider settings could not be saved to disk.", "error")


class DiscardTests(SettingsDialogTestCase):
    def _mutate(self):
        self.select_provider("Xiaomi MiMo")
        self.select_variant("Token Plan \u2014 China (CN)")
        self.w["key_var"].set("tp-mutated")
        self.w["model_var"].set("mimo-v2.6-pro")

    def test_escape_discards_every_change_without_saving(self):
        before = dict(self.app.config)
        self._mutate()
        dialog = self.w["dialog"]
        # Platform-independent pin: the Escape binding must be registered.
        self.assertTrue(dialog.bind("<Escape>"))
        # Real dispatch needs a mapped window (Tk drops key events on unmapped
        # ones; the dialog is transient to our withdrawn master). Un-withdraw
        # the master so the transient maps, dispatch, then restore withdrawal.
        self.root.deiconify()
        try:
            self.root.update()
            if not dialog.winfo_ismapped():
                self.skipTest("window manager did not map the dialog; "
                              "Escape key dispatch not verifiable on this session")
            dialog.event_generate("<Escape>")
            self.root.update()
        finally:
            self.root.withdraw()
        self.save_mock.assert_not_called()
        self.assertEqual(self.app.config, before)
        self.assertEqual(self.app.settings_widgets, {})

    def test_cancel_button_discards_every_change_without_saving(self):
        before = dict(self.app.config)
        self._mutate()
        self.w["cancel_btn"].invoke()
        self.root.update()
        self.save_mock.assert_not_called()
        self.assertEqual(self.app.config, before)
        self.assertEqual(self.app.settings_widgets, {})


class CopyAndHintTests(SettingsDialogTestCase):
    def test_mimo_hint_copy_is_the_approved_string(self):
        self.assertEqual(
            settings_view.MIMO_MODEL_HINT,
            "TTS models are not listed: WhisperLive never synthesizes speech. "
            "mimo-v2.5-asr officially supports Chinese and English only.")
        # and the label actually exists inside the split rows
        labels = [child for child in self.w["mimo_rows"].winfo_children()
                  if isinstance(child, tk.Label)
                  and child.cget("text") == settings_view.MIMO_MODEL_HINT]
        self.assertEqual(len(labels), 1)

    def test_base_url_hint_visible_for_presets_hidden_for_custom(self):
        self.assertEqual(
            settings_view.BASE_URL_HINT,
            "Preset URL \u2014 choose Custom (OpenAI-compatible URL) to edit it.")
        self.assertFalse(self.packed(self.w["base_hint"]))  # hydrated as custom
        self.select_provider("OpenAI")
        self.assertTrue(self.packed(self.w["base_hint"]))
        self.select_provider("Custom (OpenAI-compatible URL)")
        self.assertFalse(self.packed(self.w["base_hint"]))

    def test_key_status_hint_is_pure_and_scoped(self):
        # token-plan variant + empty key -> registry tp-xxxxx copy
        self.assertEqual(
            settings_view.key_status_hint("xiaomi-mimo", "token-plan-ams", ""),
            providers.get_provider("xiaomi-mimo").api_key_hint)
        # generic key-required preset + empty key -> A5A7 section 3 copy
        self.assertEqual(
            settings_view.key_status_hint("openai", "", ""),
            "An API key is required by this provider; add one before recording.")
        # filled key / keyless presets -> silent
        self.assertEqual(settings_view.key_status_hint("openai", "", "sk-x"), "")
        self.assertEqual(settings_view.key_status_hint("ollama-local", "", ""), "")
        self.assertEqual(settings_view.key_status_hint("custom", "", ""), "")
        self.assertEqual(settings_view.key_status_hint("nope", "", ""), "")
        # malformed input never raises
        self.assertEqual(settings_view.key_status_hint(None, None, None), "")


if __name__ == "__main__":
    unittest.main()
