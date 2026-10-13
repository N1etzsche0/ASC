/* Diagnostic adapter for JADX 1.5.6; forces its existing JDK ZIP fallback.
 * No target classes are loaded or executed. Compile against jadx-*-all.jar.
 */
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.TreeSet;

import jadx.api.JadxDecompiler;
import jadx.api.impl.NoOpCodeCache;
import jadx.api.impl.SimpleCodeWriter;
import jadx.api.usage.impl.EmptyUsageInfoCache;
import jadx.cli.JadxAppCommon;
import jadx.cli.JadxCLIArgs;
import jadx.cli.LogHelper;
import jadx.cli.plugins.JadxFilesGetter;
import jadx.plugins.tools.JadxExternalPluginsLoader;
import jadx.zip.ZipReaderFlags;

public class JadxLargeJar {
    public static void main(String[] argv) throws Exception {
        if (argv.length < 3 || !(argv[0].equals("jdk") || argv[0].equals("default"))) {
            throw new IllegalArgumentException("Usage: JadxLargeJar jdk|default loaded-origins.txt <JADX CLI args>");
        }
        JadxCLIArgs cli = new JadxCLIArgs();
        if (!cli.processArgs(Arrays.copyOfRange(argv, 2, argv.length))) {
            System.exit(1);
        }
        LogHelper.initLogLevel(cli);
        var args = cli.toJadxArgs();
        // Match the CLI's caches, writer, plugin discovery and environment.
        args.setCodeCache(new NoOpCodeCache());
        args.setUsageInfoCache(new EmptyUsageInfoCache());
        args.setCodeWriterProvider(SimpleCodeWriter::new);
        args.setPluginLoader(new JadxExternalPluginsLoader());
        args.setFilesGetter(JadxFilesGetter.INSTANCE);
        JadxAppCommon.applyEnvVars(args);
        int errors;
        try (JadxDecompiler jadx = new JadxDecompiler(args)) {
            if (argv[0].equals("jdk")) {
                jadx.getZipReader().getOptions().getFlags().add(ZipReaderFlags.FALLBACK_AS_DEFAULT);
            }
            jadx.load();
            TreeSet<String> origins = new TreeSet<>();
            for (var cls : jadx.getRoot().getClasses()) {
                if (cls.getClsData() != null) {
                    origins.add(cls.getInputFileName());
                }
            }
            Files.write(Path.of(argv[1]), origins);
            System.out.println("{\"stage\":\"loaded\",\"loaded_origins\":" + origins.size() + "}");
            jadx.save();
            errors = jadx.getErrorsCount();
            jadx.printErrorsReport();
            System.out.println("{\"stage\":\"finished\",\"errors\":" + errors
                    + ",\"warnings\":" + jadx.getWarnsCount() + "}");
        }
        System.exit(errors == 0 ? 0 : 3);
    }
}
