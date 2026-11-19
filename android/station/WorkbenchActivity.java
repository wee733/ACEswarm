package dev.aceswarm.h16;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.util.LruCache;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebViewDatabase;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.URLConnection;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public class WorkbenchActivity extends Activity {
    static final String HOST = "workbench.aceswarm.invalid";
    static final String ORIGIN = "https://" + HOST;
    private WebView desktop;
    private AlertDialog quitDialog;
    private AlertDialog clearDialog;
    private final LruCache<String, byte[]> iconCache = new LruCache<String, byte[]>(4 * 1024 * 1024) {
        @Override protected int sizeOf(String key, byte[] value) { return value.length; }
    };

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        WebView.setWebContentsDebuggingEnabled(getPackageName().equals(
            Settings.Global.getString(getContentResolver(), Settings.Global.DEBUG_APP)));
        desktop = new WebView(this);
        configureWebView(desktop);
        desktop.setWebChromeClient(new WebChromeClient());
        desktop.setWebViewClient(new Assets());
        // Only this WebView hosts a bridge. The asset handler blocks all external resources.
        desktop.addJavascriptInterface(new Device(), "AceAndroid");
        setContentView(desktop);
        applyFullscreen(this);
        desktop.loadUrl(ORIGIN + "/shell.html");
    }

    static void configureWebView(WebView view) {
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setTextZoom(100);
        settings.setUseWideViewPort(true);
    }

    static boolean isFullscreen(Context context) {
        return context.getSharedPreferences("workbench", MODE_PRIVATE).getBoolean("fullscreen", false);
    }

    static void applyFullscreen(Activity activity) {
        View decor = activity.getWindow().getDecorView();
        decor.setSystemUiVisibility(isFullscreen(activity)
            ? View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            : View.SYSTEM_UI_FLAG_VISIBLE);
    }

    private final class Assets extends WebViewClient {
        @Override public boolean shouldOverrideUrlLoading(WebView w, WebResourceRequest request) {
            return true;
        }
        @Override public boolean shouldOverrideUrlLoading(WebView w, String url) {
            return true;
        }
        @Override public WebResourceResponse shouldInterceptRequest(WebView w, WebResourceRequest req) {
            Uri uri = req.getUrl();
            String path = uri.getPath();
            if (!"GET".equals(req.getMethod()) || !"https".equals(uri.getScheme())
                    || !HOST.equals(uri.getEncodedAuthority()) || path == null
                    || path.contains("..") || !path.startsWith("/") || path.contains("\\")
                    || (req.isForMainFrame() && !"/shell.html".equals(path))) return missing();
            if (path.startsWith("/icons/") && path.endsWith(".png")) {
                String id = path.substring(7, path.length() - 4);
                byte[] cached = iconCache.get(id);
                if (cached != null) {
                    return new WebResourceResponse("image/png", null, new ByteArrayInputStream(cached));
                }
                try {
                    Drawable raw = getPackageManager().getApplicationIcon(id);
                    Drawable.ConstantState constant = raw.getConstantState();
                    Drawable icon = constant != null ? constant.newDrawable(getResources()).mutate() : raw.mutate();
                    Bitmap bitmap = Bitmap.createBitmap(72, 72, Bitmap.Config.ARGB_8888);
                    icon.setBounds(0, 0, bitmap.getWidth(), bitmap.getHeight());
                    icon.draw(new Canvas(bitmap));
                    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                    bitmap.compress(Bitmap.CompressFormat.PNG, 100, bytes);
                    bitmap.recycle();
                    byte[] encoded = bytes.toByteArray();
                    iconCache.put(id, encoded);
                    return new WebResourceResponse("image/png", null, new ByteArrayInputStream(encoded));
                } catch (PackageManager.NameNotFoundException | RuntimeException unavailable) { return missing(); }
            }
            try {
                InputStream input = getAssets().open(path.substring(1));
                String type = path.endsWith(".js") ? "application/javascript"
                    : path.endsWith(".css") ? "text/css" : URLConnection.guessContentTypeFromName(path);
                String charset = type != null && (type.startsWith("text/") || type.contains("javascript"))
                    ? "UTF-8" : null;
                String policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    + "img-src 'self' data:; frame-src 'none'; child-src 'none'; object-src 'none'; "
                    + "base-uri 'none'; form-action 'none'; connect-src 'none'";
                return new WebResourceResponse(type, charset, 200, "OK",
                    Collections.singletonMap("Content-Security-Policy", policy), input);
            } catch (IOException absent) { return missing(); }
        }
        private WebResourceResponse missing() {
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
        }
    }

    private final class Device {
        @JavascriptInterface public String apps() {
            PackageManager pm = getPackageManager();
            Intent query = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            List<ResolveInfo> infos = pm.queryIntentActivities(query, 0);
            Collections.sort(infos, new ResolveInfo.DisplayNameComparator(pm));
            JSONArray list = new JSONArray();
            Set<String> seen = new HashSet<>();
            for (ResolveInfo info : infos) {
                String id = info.activityInfo.packageName;
                if (id.equals(getPackageName()) || !seen.add(id)) continue;
                try {
                    JSONObject entry = new JSONObject();
                    entry.put("id", id);
                    entry.put("title", info.loadLabel(pm).toString());
                    entry.put("icon", ORIGIN + "/icons/" + id + ".png");
                    list.put(entry);
                } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
            }
            return list.toString();
        }
        @JavascriptInterface public void launch(String packageId) {
            runOnUiThread(() -> {
                if (packageId == null || getPackageName().equals(packageId)) return;
                Intent launch = getPackageManager().getLaunchIntentForPackage(packageId);
                if (launch == null) { notifyUser(R.string.unavailable); return; }
                try { startActivity(launch); }
                catch (RuntimeException e) { notifyUser(R.string.unavailable); }
            });
        }
        @JavascriptInterface public void openPage(String url) { browse(url, false); }
        @JavascriptInterface public void openStore(String url) { browse(url, true); }
        private void browse(String url, boolean store) {
            runOnUiThread(() -> {
                if (!PageActivity.isWebAddress(url)) { notifyUser(R.string.page_failed); return; }
                startActivity(new Intent(WorkbenchActivity.this, PageActivity.class).setData(Uri.parse(url))
                    .putExtra(PageActivity.STORE_MODE, store).addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
            });
        }
        @JavascriptInterface public String getSetting(String key) {
            if (!"store".equals(key) && !"appearance".equals(key)) return "";
            return getSharedPreferences("workbench", MODE_PRIVATE).getString(key, "");
        }
        @JavascriptInterface public void setSetting(String key, String value) {
            if ("store".equals(key) || "appearance".equals(key)) {
                getSharedPreferences("workbench", MODE_PRIVATE).edit().putString(key, value).apply();
            }
        }
        @JavascriptInterface public void clearBrowserData(String message, String confirmLabel, String cancelLabel) {
            runOnUiThread(() -> confirmClearBrowserData(message, confirmLabel, cancelLabel));
        }
        @JavascriptInterface public String locale() {
            return getResources().getConfiguration().getLocales().get(0).toLanguageTag();
        }
        @JavascriptInterface public void downloads() {
            runOnUiThread(() -> {
                try { startActivity(new Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)); }
                catch (RuntimeException unavailable) { notifyUser(R.string.downloads_unavailable); }
            });
        }
        @JavascriptInterface public void fullscreen() {
            runOnUiThread(() -> {
                boolean next = !isFullscreen(WorkbenchActivity.this);
                getSharedPreferences("workbench", MODE_PRIVATE).edit().putBoolean("fullscreen", next).apply();
                applyFullscreen(WorkbenchActivity.this);
            });
        }
        @JavascriptInterface public void quit() { runOnUiThread(() -> confirmQuit()); }
    }

    private void notifyDataCleared(boolean cleared) {
        if (!isDestroyed()) {
            desktop.evaluateJavascript(
                "if (window.aceAndroidDataCleared) window.aceAndroidDataCleared(" + cleared + ")", null);
        }
    }

    private void confirmClearBrowserData(String message, String confirmLabel, String cancelLabel) {
        if (isFinishing() || (clearDialog != null && clearDialog.isShowing())) {
            notifyDataCleared(false);
            return;
        }
        CharSequence prompt = (message != null && !message.isEmpty()) ? message : getString(R.string.clear_data_confirm);
        CharSequence positive = (confirmLabel != null && !confirmLabel.isEmpty()) ? confirmLabel : getString(R.string.clear_data_action);
        CharSequence negative = (cancelLabel != null && !cancelLabel.isEmpty()) ? cancelLabel : getString(android.R.string.cancel);
        final boolean[] confirmed = { false };
        clearDialog = new AlertDialog.Builder(this)
            .setMessage(prompt)
            .setNegativeButton(negative, null)
            .setPositiveButton(positive, (dialog, which) -> {
                confirmed[0] = true;
                performClearBrowserData();
            })
            .setOnDismissListener(dialog -> {
                clearDialog = null;
                applyFullscreen(this);
                if (!confirmed[0]) notifyDataCleared(false);
            })
            .show();
    }

    private void performClearBrowserData() {
        SharedPreferences browser = getSharedPreferences("browser", MODE_PRIVATE);
        browser.edit().putLong("generation", browser.getLong("generation", 0) + 1).apply();
        iconCache.evictAll();
        PageActivity.clearSessions();
        desktop.clearHistory();
        desktop.clearCache(true);
        desktop.clearFormData();
        desktop.clearSslPreferences();
        WebViewDatabase database = WebViewDatabase.getInstance(this);
        database.clearFormData();
        database.clearHttpAuthUsernamePassword();
        WebStorage.getInstance().deleteAllData();
        CookieManager cookies = CookieManager.getInstance();
        cookies.removeAllCookies(removed -> {
            cookies.flush();
            notifyDataCleared(true);
        });
    }

    private void notifyUser(int message) { Toast.makeText(this, message, Toast.LENGTH_SHORT).show(); }

    private void confirmQuit() {
        if (isFinishing() || (quitDialog != null && quitDialog.isShowing())) return;
        quitDialog = new AlertDialog.Builder(this).setMessage(R.string.quit_workbench)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.quit, (dialog, which) -> {
                ActivityManager manager = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
                for (ActivityManager.AppTask task : manager.getAppTasks()) {
                    if (task.getTaskInfo().id == getTaskId()) { task.finishAndRemoveTask(); return; }
                }
                finishAffinity();
            })
            .setOnDismissListener(dialog -> {
                quitDialog = null;
                applyFullscreen(this);
            })
            .show();
    }

    @Override public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) applyFullscreen(this);
    }

    @Override public void onBackPressed() {
        desktop.evaluateJavascript("typeof window.aceAndroidBack === 'function' && window.aceAndroidBack()",
            handled -> { if (!"true".equals(handled) && !isDestroyed()) confirmQuit(); });
    }
    @Override protected void onPause() { desktop.onPause(); super.onPause(); }
    @Override protected void onResume() {
        super.onResume();
        applyFullscreen(this);
        desktop.onResume();
        desktop.evaluateJavascript("if (typeof window.aceAndroidResume === 'function') window.aceAndroidResume()", null);
    }
    @Override protected void onDestroy() {
        if (quitDialog != null) quitDialog.dismiss();
        if (clearDialog != null) clearDialog.dismiss();
        iconCache.evictAll();
        desktop.removeJavascriptInterface("AceAndroid");
        dispose(desktop);
        super.onDestroy();
    }
    static void dispose(WebView web) {
        web.stopLoading();
        web.setWebChromeClient(null);
        web.setWebViewClient(null);
        ViewGroup parent = (ViewGroup) web.getParent();
        if (parent != null) parent.removeView(web);
        web.destroy();
    }
}
