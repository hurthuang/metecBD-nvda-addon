# globalPlugins/metecBDConfig.py
# A part of NonVisual Desktop Access (NVDA)
# Config menu for MetecBD Braille Display

import addonHandler
addonHandler.initTranslation()

import globalPluginHandler
import gui
import wx
import braille
from logHandler import log

try:
	from brailleDisplayDrivers.metecBD import load_timeout, save_timeout
except ImportError:
	# Fallback if the driver isn't loaded or path isn't active
	def load_timeout(): return 18
	def save_timeout(val): pass

class MetecConfigDialog(wx.Dialog):
	def __init__(self, parent, current_val, callback):
		# Translators: Title of the Metec BD config dialog
		super().__init__(parent, title=_("Metec BD 點字顯示器設定"), style=wx.DEFAULT_DIALOG_STYLE)
		self.callback = callback
		
		sizer = wx.BoxSizer(wx.VERTICAL)
		
		# Translators: Label in the Metec BD config dialog
		label = wx.StaticText(self, label=_("請輸入閒置自動休眠時間（秒，設定為 0 代表停用休眠）："))
		sizer.Add(label, 0, wx.ALL, 10)
		
		self.spin = wx.SpinCtrl(self, min=0, max=86400, initial=current_val)
		sizer.Add(self.spin, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		
		# Buttons
		btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
		
		self.SetSizerAndFit(sizer)
		self.Center()
		
		self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
		
	def on_ok(self, event):
		val = self.spin.GetValue()
		self.callback(val)
		event.Skip()

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.menuItem = None
		# wx.CallAfter is used because the GUI may not be fully initialized when the plugin is loaded
		wx.CallAfter(self._addMenuItem)

	def _addMenuItem(self):
		try:
			sysTrayIcon = gui.mainFrame.sysTrayIcon
			toolsMenu = sysTrayIcon.toolsMenu
			# Translators: Label for the Metec BD setting menu item under Tools menu
			self.menuItem = toolsMenu.Append(
				wx.ID_ANY,
				_("Metec BD 點字顯示器設定(&M)..."),
				_("設定 Metec BD 點字顯示器的休眠時間與選項"),
			)
			sysTrayIcon.Bind(wx.EVT_MENU, self.onSettingsMenu, self.menuItem)
		except Exception:
			log.exception("MetecBD Config Plugin: Failed to add menu item")

	def terminate(self, *args, **kwargs):
		try:
			if self.menuItem:
				sysTrayIcon = gui.mainFrame.sysTrayIcon
				toolsMenu = sysTrayIcon.toolsMenu
				toolsMenu.Remove(self.menuItem.Id)
				self.menuItem.Destroy()
		except Exception:
			pass
		super().terminate(*args, **kwargs)

	def onSettingsMenu(self, event):
		current_val = load_timeout()
		
		def callback(val):
			save_timeout(val)
			# Apply to running display driver if active
			try:
				display = braille.handler.display
				if display and display.name == "metecBD" and hasattr(display, "set_idle_timeout"):
					display.set_idle_timeout(val)
			except Exception:
				log.exception("MetecBD Config Plugin: Failed to apply new timeout to running driver")

		dlg = MetecConfigDialog(gui.mainFrame, current_val, callback)
		dlg.ShowModal()
		dlg.Destroy()
