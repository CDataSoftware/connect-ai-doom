// The full-screen layout's backdrop: one borderless window over the whole main screen,
// showing the side panel's stage page in a web view, with the menu bar and the Dock hidden.
// raid.sh starts it, then places the game's borderless window on the page's game box.
//   osascript -l JavaScript tools/stage.js <url>
// Cmd-Q closes it; raid.sh also stops it when the game ends.
ObjC.import("Cocoa");
ObjC.import("WebKit");

// A borderless window can't take clicks and keys unless it says it can.
ObjC.registerSubclass({name: "StageWindow", superclass: "NSWindow", methods: {
  "canBecomeKeyWindow": {types: ["bool", []], implementation: () => true},
}});
// Presentation options only apply while this app is in front, so set them each time it is.
ObjC.registerSubclass({name: "StageDelegate", superclass: "NSObject", protocols: ["NSApplicationDelegate"], methods: {
  "applicationDidBecomeActive:": {types: ["void", ["id"]], implementation: () => {
    $.NSApplication.sharedApplication.presentationOptions = 2 | 8;  // hide the Dock and the menu bar
  }},
}});

function run(argv) {
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyRegular);
  app.delegate = $.StageDelegate.alloc.init;

  const menu = $.NSMenu.alloc.init, item = $.NSMenuItem.alloc.init, sub = $.NSMenu.alloc.init;
  sub.addItem($.NSMenuItem.alloc.initWithTitleActionKeyEquivalent("Quit", "terminate:", "q"));
  item.submenu = sub;
  menu.addItem(item);
  app.mainMenu = menu;

  const frame = $.NSScreen.mainScreen.frame;
  const win = $.StageWindow.alloc.initWithContentRectStyleMaskBackingDefer(
    frame, $.NSWindowStyleMaskBorderless, $.NSBackingStoreBuffered, false);
  win.backgroundColor = $.NSColor.blackColor;
  const view = $.WKWebView.alloc.initWithFrameConfiguration(
    $.NSMakeRect(0, 0, frame.size.width, frame.size.height), $.WKWebViewConfiguration.alloc.init);
  view.setValueForKey($.NSNumber.numberWithBool(false), "drawsBackground");  // black, not white, while it loads
  view.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(argv[0])));
  win.contentView = view;
  win.makeKeyAndOrderFront(null);
  app.activateIgnoringOtherApps(true);
  app.run;
}
