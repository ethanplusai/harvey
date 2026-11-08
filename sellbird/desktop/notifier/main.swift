// Sellbird's notifier: posts one macOS notification with Sellbird's own name
// and icon, and opens the dashboard when it's clicked.
//
//   open -n -g Sellbird.app --args "<message>" [<file holding the URL to open on click>]
//
// Run with no arguments (macOS relaunches it when a notification is
// clicked), it only waits for the click and opens that notification's URL.
import Cocoa
import UserNotifications

final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let args = Array(CommandLine.arguments.dropFirst())

    func applicationDidFinishLaunching(_ note: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        guard let message = args.first, !message.isEmpty else {
            quit(after: 5)   // launched by a click: wait for didReceive
            return
        }
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { self.quit(after: 0); return }
            let content = UNMutableNotificationContent()
            content.title = "Sellbird"
            content.body = message
            content.sound = .default
            if self.args.count > 1, !self.args[1].isEmpty { content.userInfo = ["url": self.args[1]] }
            let request = UNNotificationRequest(identifier: UUID().uuidString,
                                                content: content, trigger: nil)
            center.add(request) { _ in self.quit(after: 1) }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        // The tray passes a file holding the dashboard's address (it carries
        // your token, so it never goes on a command line).
        if let path = response.notification.request.content.userInfo["url"] as? String,
           let link = try? String(contentsOfFile: path, encoding: .utf8),
           let url = URL(string: link.trimmingCharacters(in: .whitespacesAndNewlines)) {
            NSWorkspace.shared.open(url)
        }
        done()
        quit(after: 0.5)
    }

    func quit(after seconds: Double) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { NSApp.terminate(nil) }
    }
}

let app = NSApplication.shared
let delegate = Notifier()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
