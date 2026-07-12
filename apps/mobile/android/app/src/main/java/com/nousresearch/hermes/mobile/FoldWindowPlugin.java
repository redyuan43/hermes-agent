package com.nousresearch.hermes.mobile;

import android.app.Activity;
import android.graphics.Rect;
import android.util.DisplayMetrics;

import androidx.core.util.Consumer;
import androidx.window.java.layout.WindowInfoTrackerCallbackAdapter;
import androidx.window.layout.DisplayFeature;
import androidx.window.layout.FoldingFeature;
import androidx.window.layout.WindowInfoTracker;
import androidx.window.layout.WindowLayoutInfo;
import androidx.window.layout.WindowMetricsCalculator;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.concurrent.Executor;

@CapacitorPlugin(name = "FoldWindow")
public class FoldWindowPlugin extends Plugin {
  private final Executor mainExecutor = Runnable::run;
  private WindowInfoTrackerCallbackAdapter tracker;
  private Consumer<WindowLayoutInfo> listener;
  private WindowLayoutInfo latestLayoutInfo;

  @Override
  public void load() {
    Activity activity = getActivity();
    tracker = new WindowInfoTrackerCallbackAdapter(WindowInfoTracker.getOrCreate(activity));
    listener = info -> {
      latestLayoutInfo = info;
      notifyListeners("changed", stateFor(activity, info));
    };
    tracker.addWindowLayoutInfoListener(activity, mainExecutor, listener);
  }

  @Override
  protected void handleOnDestroy() {
    if (tracker != null && listener != null) {
      tracker.removeWindowLayoutInfoListener(listener);
    }
  }

  @PluginMethod
  public void getState(PluginCall call) {
    call.resolve(stateFor(getActivity(), latestLayoutInfo));
  }

  private JSObject stateFor(Activity activity, WindowLayoutInfo layoutInfo) {
    DisplayMetrics metrics = activity.getResources().getDisplayMetrics();
    float density = Math.max(1f, metrics.density);
    Rect bounds = WindowMetricsCalculator.getOrCreate()
      .computeCurrentWindowMetrics(activity)
      .getBounds();

    JSObject result = new JSObject();
    int widthDp = Math.round(bounds.width() / density);
    int heightDp = Math.round(bounds.height() / density);
    result.put("widthDp", widthDp);
    result.put("heightDp", heightDp);
    result.put("displayRole", widthDp < 480 ? "cover" : widthDp >= 600 ? "inner" : "unknown");
    result.put("posture", "flat");
    result.put("isSeparating", false);
    result.put("foldBounds", null);

    if (layoutInfo == null) {
      return result;
    }

    for (DisplayFeature feature : layoutInfo.getDisplayFeatures()) {
      if (!(feature instanceof FoldingFeature)) {
        continue;
      }

      FoldingFeature folding = (FoldingFeature) feature;
      Rect foldBounds = folding.getBounds();
      JSObject fold = new JSObject();
      fold.put("left", Math.round(foldBounds.left / density));
      fold.put("top", Math.round(foldBounds.top / density));
      fold.put("right", Math.round(foldBounds.right / density));
      fold.put("bottom", Math.round(foldBounds.bottom / density));
      result.put("foldBounds", fold);
      result.put("isSeparating", folding.isSeparating());
      result.put(
        "posture",
        FoldingFeature.State.HALF_OPENED.equals(folding.getState()) ? "half-opened" : "flat"
      );
      break;
    }

    return result;
  }
}
