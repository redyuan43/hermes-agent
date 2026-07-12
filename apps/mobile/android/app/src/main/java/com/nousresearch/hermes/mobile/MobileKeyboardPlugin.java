package com.nousresearch.hermes.mobile;

import android.content.Context;
import android.view.View;
import android.view.inputmethod.InputMethodManager;

import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "MobileKeyboard")
public class MobileKeyboardPlugin extends Plugin {
  @PluginMethod
  public void show(PluginCall call) {
    getActivity().runOnUiThread(() -> {
      View webView = getBridge().getWebView();
      webView.requestFocus();

      WindowInsetsControllerCompat controller = ViewCompat.getWindowInsetsController(webView);
      if (controller != null) {
        controller.show(WindowInsetsCompat.Type.ime());
      }

      InputMethodManager inputMethodManager = (InputMethodManager) getActivity()
        .getSystemService(Context.INPUT_METHOD_SERVICE);
      inputMethodManager.showSoftInput(webView, InputMethodManager.SHOW_IMPLICIT);
      call.resolve();
    });
  }
}
