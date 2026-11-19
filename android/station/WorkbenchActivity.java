package dev.aceswarm.h16;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.AlertDialog;
import android.app.DownloadManager;
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
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.CookieManager;
import android.webkit.WebStorage;
import android.webkit.WebViewDatabase;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
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
                try {
                    Drawable icon = getPackageManager().getApplicationIcon(id);
                    Bitmap bitmap = Bitmap.createBitmap(72, 72, Bitmap.Config.ARGB_8888);
                    icon.setBounds(0, 0, bitmap.getWidth(), bitmap.getHeight());
                    icon.draw(new Canvas(bitmap));
                    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                    bitmap.compress(Bitmap.CompressFormat.PNG, 100, bytes);
                    bitmap.recycle();
                    return new WebResourceResponse("image/png", null,
                        new ByteArrayInputStream(bytes.toByteArray()));
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
        @JavascriptInterface public void clearBrowserData() {
            runOnUiThread(() -> {
                SharedPreferences browser = getSharedPreferences("browser", MODE_PRIVATE);
                browser.edit().putLong("generation", browser.getLong("generation", 0) + 1).apply();
                PageActivity.clearSessions();
                desktop.clearHistory();
                desktop.clearCache(true);
                desktop.clearFormData();
                desktop.clearSslPreferences();
                WebViewDatabase database = WebViewDatabase.getInstance(WorkbenchActivity.this);
                database.clearFormData();
                database.clearHttpAuthUsernamePassword();
                WebStorage.getInstance().deleteAllData();
                CookieManager cookies = CookieManager.getInstance();
                cookies.removeAllCookies(removed -> {
                    cookies.flush();
                    if (!isDestroyed()) desktop.evaluateJavascript(
                        "if (window.aceAndroidDataCleared) window.aceAndroidDataCleared()", null);
                });
            });
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
                View decor = getWindow().getDecorView();
                boolean visible = (decor.getSystemUiVisibility() & View.SYSTEM_UI_FLAG_FULLSCREEN) == 0;
                decor.setSystemUiVisibility(visible ? View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY : 0);
            });
        }
        @JavascriptInterface public void quit() { runOnUiThread(() -> confirmQuit()); }
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
            }).show();
    }

    @Override public void onBackPressed() {
        desktop.evaluateJavascript("typeof window.aceAndroidBack === 'function' && window.aceAndroidBack()",
            handled -> { if (!"true".equals(handled) && !isDestroyed()) confirmQuit(); });
    }
    @Override protected void onPause() { desktop.onPause(); super.onPause(); }
    @Override protected void onResume() {
        super.onResume();
        desktop.onResume();
        desktop.evaluateJavascript("if (typeof window.aceAndroidResume === 'function') window.aceAndroidResume()", null);
    }
    @Override protected void onDestroy() {
        if (quitDialog != null) quitDialog.dismiss();
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
