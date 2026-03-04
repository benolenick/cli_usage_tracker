# cli_status_reader.ps1
# Spawns a CLI TUI (Codex/Gemini), sends a slash command, reads console screen buffer.
# Usage: powershell -ExecutionPolicy Bypass -File cli_status_reader.ps1 -CliExe <path> -Command "/status" [-CliArgs <args>] [-InitWait 12] [-PostWait 5]

param(
    [Parameter(Mandatory=$true)][string]$CliExe,
    [string]$CliArgs = "",
    [Parameter(Mandatory=$true)][string]$Command,
    [int]$InitWait = 12,
    [int]$PostWait = 5,
    [string]$OutFile = "",
    [switch]$BatchInput
)

Add-Type @"
using System;
using System.Runtime.InteropServices;

public class WC {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AttachConsole(uint dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool FreeConsole();

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern IntPtr CreateFile(
        string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        IntPtr lpSecurityAttributes, uint dwCreationDisposition,
        uint dwFlagsAndAttributes, IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetConsoleScreenBufferInfo(IntPtr h, out CSBI info);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool ReadConsoleOutputCharacter(
        IntPtr h, [Out] char[] buf, uint len, COORD coord, out uint read);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool WriteConsoleInput(
        IntPtr h, INPUT_RECORD[] buf, uint len, out uint written);

    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr h);

    [DllImport("user32.dll")]
    public static extern short VkKeyScan(char ch);

    public const uint GR = 0x80000000, GW = 0x40000000, SR = 1, SW = 2, OE = 3;

    [StructLayout(LayoutKind.Sequential)]
    public struct COORD { public short X; public short Y; }

    [StructLayout(LayoutKind.Sequential)]
    public struct SMALL_RECT { public short L, T, R, B; }

    [StructLayout(LayoutKind.Sequential)]
    public struct CSBI {
        public COORD dwSize; public COORD dwCursor; public ushort wAttr;
        public SMALL_RECT srWindow; public COORD dwMax;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct INPUT_RECORD {
        [FieldOffset(0)] public ushort EventType;
        [FieldOffset(4)] public KEY_EVENT_RECORD KeyEvent;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct KEY_EVENT_RECORD {
        public int bKeyDown; public ushort wRepeatCount; public ushort wVirtualKeyCode;
        public ushort wVirtualScanCode; public char UnicodeChar; public uint dwControlKeyState;
    }

    public static IntPtr OpenConOut() {
        return CreateFile("CONOUT$", GR|GW, SR|SW, IntPtr.Zero, OE, 0, IntPtr.Zero);
    }
    public static IntPtr OpenConIn() {
        return CreateFile("CONIN$", GR|GW, SR|SW, IntPtr.Zero, OE, 0, IntPtr.Zero);
    }

    public static void SendKey(IntPtr hIn, char ch, ushort vk, ushort scan) {
        INPUT_RECORD[] recs = new INPUT_RECORD[2];
        recs[0].EventType = 1;
        recs[0].KeyEvent.bKeyDown = 1;
        recs[0].KeyEvent.wRepeatCount = 1;
        recs[0].KeyEvent.wVirtualKeyCode = vk;
        recs[0].KeyEvent.wVirtualScanCode = scan;
        recs[0].KeyEvent.UnicodeChar = ch;
        recs[0].KeyEvent.dwControlKeyState = 0;
        recs[1].EventType = 1;
        recs[1].KeyEvent.bKeyDown = 0;
        recs[1].KeyEvent.wRepeatCount = 1;
        recs[1].KeyEvent.wVirtualKeyCode = vk;
        recs[1].KeyEvent.wVirtualScanCode = scan;
        recs[1].KeyEvent.UnicodeChar = ch;
        recs[1].KeyEvent.dwControlKeyState = 0;
        uint written;
        WriteConsoleInput(hIn, recs, 2, out written);
    }

    public static void SendChar(IntPtr hIn, char ch) {
        short vkResult = VkKeyScan(ch);
        ushort vk = (ushort)(vkResult & 0xFF);
        SendKey(hIn, ch, vk, 0);
    }

    public static void SendEnter(IntPtr hIn) {
        SendKey(hIn, '\r', 0x0D, 0x1C);
    }

    public static void SendString(IntPtr hIn, string text) {
        foreach (char c in text) {
            SendChar(hIn, c);
            System.Threading.Thread.Sleep(30);
        }
    }

    public static void SendStringBatch(IntPtr hIn, string text, bool withEnter) {
        // Send all chars + optional Enter as a single WriteConsoleInput call (paste-style).
        // This prevents TUI autocomplete from intercepting between keystrokes.
        int evCount = text.Length * 2 + (withEnter ? 2 : 0);
        INPUT_RECORD[] recs = new INPUT_RECORD[evCount];
        int idx = 0;
        foreach (char c in text) {
            short vkResult = VkKeyScan(c);
            ushort vk = (ushort)(vkResult & 0xFF);
            recs[idx].EventType = 1;
            recs[idx].KeyEvent.bKeyDown = 1;
            recs[idx].KeyEvent.wRepeatCount = 1;
            recs[idx].KeyEvent.wVirtualKeyCode = vk;
            recs[idx].KeyEvent.wVirtualScanCode = 0;
            recs[idx].KeyEvent.UnicodeChar = c;
            recs[idx].KeyEvent.dwControlKeyState = 0;
            idx++;
            recs[idx].EventType = 1;
            recs[idx].KeyEvent.bKeyDown = 0;
            recs[idx].KeyEvent.wRepeatCount = 1;
            recs[idx].KeyEvent.wVirtualKeyCode = vk;
            recs[idx].KeyEvent.wVirtualScanCode = 0;
            recs[idx].KeyEvent.UnicodeChar = c;
            recs[idx].KeyEvent.dwControlKeyState = 0;
            idx++;
        }
        if (withEnter) {
            recs[idx].EventType = 1;
            recs[idx].KeyEvent.bKeyDown = 1;
            recs[idx].KeyEvent.wRepeatCount = 1;
            recs[idx].KeyEvent.wVirtualKeyCode = 0x0D;
            recs[idx].KeyEvent.wVirtualScanCode = 0x1C;
            recs[idx].KeyEvent.UnicodeChar = '\r';
            recs[idx].KeyEvent.dwControlKeyState = 0;
            idx++;
            recs[idx].EventType = 1;
            recs[idx].KeyEvent.bKeyDown = 0;
            recs[idx].KeyEvent.wRepeatCount = 1;
            recs[idx].KeyEvent.wVirtualKeyCode = 0x0D;
            recs[idx].KeyEvent.wVirtualScanCode = 0x1C;
            recs[idx].KeyEvent.UnicodeChar = '\r';
            recs[idx].KeyEvent.dwControlKeyState = 0;
            idx++;
        }
        uint written;
        WriteConsoleInput(hIn, recs, (uint)evCount, out written);
    }

    public static string ReadScreen(IntPtr hOut) {
        CSBI info;
        if (!GetConsoleScreenBufferInfo(hOut, out info))
            return "ERROR: GetConsoleScreenBufferInfo failed: " + Marshal.GetLastWin32Error();
        int w = info.dwSize.X;
        string result = "";
        for (int row = info.srWindow.T; row <= info.srWindow.B; row++) {
            char[] chars = new char[w];
            COORD coord; coord.X = 0; coord.Y = (short)row;
            uint read;
            ReadConsoleOutputCharacter(hOut, chars, (uint)w, coord, out read);
            result += new String(chars).TrimEnd() + "\n";
        }
        return result;
    }
}
"@

$errorOut = ""

try {
    # Start the CLI minimized
    if ($CliArgs) {
        $proc = Start-Process -FilePath $CliExe -ArgumentList $CliArgs -WindowStyle Minimized -PassThru
    } else {
        $proc = Start-Process -FilePath $CliExe -WindowStyle Minimized -PassThru
    }

    # Wait for TUI to initialize
    Start-Sleep -Seconds $InitWait

    if ($proc.HasExited) {
        $errorOut = "ERROR: Process exited prematurely with code $($proc.ExitCode)"
        [System.IO.File]::WriteAllText($OutFile, $errorOut, [System.Text.Encoding]::UTF8)
        exit 1
    }

    # Attach to its console
    [WC]::FreeConsole() | Out-Null
    $attached = [WC]::AttachConsole([uint32]$proc.Id)
    if (-not $attached) {
        $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        $errorOut = "ERROR: AttachConsole failed: $err"
        if ($OutFile) {
            [System.IO.File]::WriteAllText($OutFile, $errorOut, [System.Text.Encoding]::UTF8)
        }
        $proc.Kill()
        exit 1
    }

    $hOut = [WC]::OpenConOut()
    $hIn = [WC]::OpenConIn()

    # Type the command
    if ($BatchInput) {
        # Send all chars + Enter as single batch (prevents TUI autocomplete interference)
        [WC]::SendStringBatch($hIn, $Command, $true)
    } else {
        [WC]::SendString($hIn, $Command)
        Start-Sleep -Seconds 2
        [WC]::SendEnter($hIn)
    }

    # Wait for output to render
    Start-Sleep -Seconds $PostWait

    # Read screen buffer
    $screen = [WC]::ReadScreen($hOut)

    # Output
    if ($OutFile) {
        [System.IO.File]::WriteAllText($OutFile, $screen, [System.Text.Encoding]::UTF8)
    }

    # Cleanup
    [WC]::CloseHandle($hOut) | Out-Null
    [WC]::CloseHandle($hIn) | Out-Null
    [WC]::FreeConsole() | Out-Null
    $proc.Kill()
}
catch {
    $errorOut = "ERROR: $_"
    if ($OutFile) {
        [System.IO.File]::WriteAllText($OutFile, $errorOut, [System.Text.Encoding]::UTF8)
    }
    try { $proc.Kill() } catch {}
    exit 1
}
