/* ASC's framed, sequential host for the unmodified CFR entry point. */
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;

public final class CfrWorker {
    private static final int MAGIC = 0x41534331;
    private static final int MAX_TEXT = 65536;

    /* Keep diagnostics bounded, but recognize heap failures before tail truncation. */
    private static final class Diagnostics extends OutputStream {
        private final byte[] tail = new byte[MAX_TEXT];
        private final String[] patterns = {
            "java.lang.OutOfMemoryError: Java heap space",
            "java.lang.OutOfMemoryError: GC overhead limit exceeded"
        };
        private final int[] matches = new int[patterns.length];
        private int position, size;
        boolean heap;

        @Override public void write(int value) {
            tail[position] = (byte)value;
            position = (position + 1) % tail.length;
            size = Math.min(size + 1, tail.length);
            for (int i = 0; i < patterns.length; i++) {
                String pattern = patterns[i];
                int matched = matches[i];
                matched = value == pattern.charAt(matched) ? matched + 1 :
                    (value == pattern.charAt(0) ? 1 : 0);
                if (matched == pattern.length()) { heap = true; matched = 0; }
                matches[i] = matched;
            }
        }

        byte[] bytes() {
            byte[] result = new byte[size];
            int start = (position - size + tail.length) % tail.length;
            for (int i = 0; i < size; i++) result[i] = tail[(start + i) % tail.length];
            return result;
        }
    }

    public static void main(String[] ignored) throws Exception {
        DataInputStream input = new DataInputStream(new BufferedInputStream(System.in));
        DataOutputStream output = new DataOutputStream(new BufferedOutputStream(System.out));
        PrintStream bootstrap = System.err;
        // Only CFR is loaded. Target bytecode is never loaded into this JVM.
        Method main = Class.forName("org.benf.cfr.reader.Main").getMethod("main", String[].class);
        output.writeInt(MAGIC);
        output.flush();
        while (true) {
            int count;
            try { count = input.readInt(); } catch (EOFException finished) { break; }
            if (count < 1 || count > 1024) throw new IOException("Invalid argument count");
            String[] args = new String[count];
            for (int i = 0; i < count; i++) {
                int length = input.readInt();
                if (length < 0 || length > MAX_TEXT) throw new IOException("Invalid argument length");
                byte[] value = new byte[length];
                input.readFully(value);
                args[i] = new String(value, StandardCharsets.UTF_8);
            }
            Diagnostics diagnostics = new Diagnostics();
            PrintStream captured = new PrintStream(diagnostics, true, "UTF-8");
            System.setOut(captured);
            System.setErr(captured);
            int status = 0;
            boolean fatal = false;
            try {
                // This uses the same options and per-root state reset as CFR CLI.
                main.invoke(null, (Object)args);
            } catch (InvocationTargetException exception) {
                Throwable cause = exception.getCause();
                fatal = cause instanceof VirtualMachineError || cause instanceof ThreadDeath;
                cause.printStackTrace(captured);
                status = 1;
            } catch (Throwable exception) {
                exception.printStackTrace(captured);
                status = 1;
                fatal = true;
            } finally {
                captured.flush();
                System.setOut(bootstrap);
                System.setErr(bootstrap);
            }
            byte[] text = diagnostics.bytes();
            output.writeInt(status);
            output.writeInt(diagnostics.heap ? 1 : 0);
            output.writeInt(text.length);
            output.write(text);
            output.flush();
            if (fatal || diagnostics.heap) break;
        }
    }
}
