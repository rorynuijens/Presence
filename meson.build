project('presence', version: '1.0.0')

install_data('io.github.presence.svg',
  install_dir: join_paths(get_option('datadir'), 'icons/hicolor/scalable/apps')
)

install_data('io.github.presence.desktop',
  install_dir: join_paths(get_option('datadir'), 'applications')
)

install_data('data/io.github.presence.metainfo.xml',
  install_dir: join_paths(get_option('datadir'), 'metainfo')
)

python = import('python')
py = python.find_installation()

py.install_sources(
  run_command('find', 'src/presence', '-maxdepth', '1', '-name', '*.py', check: true).stdout().strip().split('\n'),
  subdir: 'presence'
)
py.install_sources(
  run_command('find', 'src/presence/slides', '-name', '*.py', check: true).stdout().strip().split('\n'),
  subdir: 'presence/slides'
)

install_data('src/presence/presence-markdown.xml',
  install_dir: join_paths(get_option('datadir'), 'gtksourceview-5/styles')
)