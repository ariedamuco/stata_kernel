import base64

from PIL import Image
from xml.etree import ElementTree
from ipykernel.kernelbase import Kernel

from .config import Config
from .completions import CompletionsManager
from .code_manager import CodeManager
from .stata_session import StataSession
from .stata_magics import StataMagics


class StataKernel(Kernel):
    implementation = 'stata_kernel'
    implementation_version = '1.3.1'
    language = 'stata'
    language_version = '15.1'
    language_info = {
        'name': 'stata',
        'mimetype': 'text/x-stata',
        'file_extension': '.do'}

    def __init__(self, *args, **kwargs):
        super(StataKernel, self).__init__(*args, **kwargs)

        # Can't name this `self.config`. Conflicts with a Jupyter attribute
        self.conf = Config()

        self.graphs = {}
        self.magics = StataMagics()
        self.sc_delimit_mode = False
        self.stata = StataSession(self, self.conf)
        self.completions = CompletionsManager(self, self.conf)
        self.banner = self.stata.banner

    def do_execute(
            self, code, silent, store_history=True, user_expressions=None,
            allow_stdin=False):
        """Execute user code.

        This is the function that Jupyter calls to run code. Must return a
        dictionary as described here:
        https://jupyter-client.readthedocs.io/en/stable/messaging.html#execution-results

        """
        if not self.is_complete(code):
            return {'status': 'error', 'execution_count': self.execution_count}

        # Search for magics in the code
        code = self.magics.magic(code, self)

        # If the magic executed, bail out early
        if self.magics.quit_early:
            return self.magics.quit_early

        # Tokenize code and return code chunks
        cm = CodeManager(code, self.sc_delimit_mode)
        text_to_run, md5, text_to_exclude = cm.get_text(self.conf)
        rc, res = self.stata.do(text_to_run, md5, self.magics, text_to_exclude=text_to_exclude)

        # Post magic results, if applicable
        self.magics.post(self)

        # The base class increments the execution count
        return_obj = {'execution_count': self.execution_count}
        if rc:
            return_obj['status'] = 'error'
        else:
            return_obj['status'] = 'ok'
            return_obj['payload'] = []
            return_obj['user_expressions'] = {}

        if silent:
            # Refresh completions
            self.completions.refresh(self)
            return return_obj

        # Send message if delimiter changed. NOTE: This uses the delimiter at
        # the _end_ of the code block. It prints only if the delimiter at the
        # end is different than the one before the chunk.
        if cm.ends_sc != self.sc_delimit_mode:
            delim = ';' if cm.ends_sc else 'cr'
            self.send_response(
                self.iopub_socket, 'stream', {
                    'text': 'delimiter now {}'.format(delim),
                    'name': 'stdout'})
        self.sc_delimit_mode = cm.ends_sc

        # Refresh completions
        self.completions.refresh(self)
        return return_obj

    def send_image(self, graph_path):
        """Load graph

        Args:
            graph_path (str): path to exported graph

        Returns:
            None. Sends output to frontend
        """

        # graph_path = '/Users/Kyle/.stata_kernel_cache/Graph1.png'
        no_display_msg = 'This front-end cannot display the desired image type.'
        if graph_path.endswith('.svg'):
            e = ElementTree.parse(graph_path)
            root = e.getroot()

            content = {
                'data': {
                    'text/plain': no_display_msg,
                    'image/svg+xml': ElementTree.tostring(root).decode('utf-8')
                },
                'metadata': {
                    'image/svg+xml': {
                        'width': int(root.attrib['width'][:-2]),
                        'height': int(root.attrib['height'][:-2])
                    }
                }
            }
            self.send_response(self.iopub_socket, 'display_data', content)
        elif graph_path.endswith('.png'):
            im = Image.open(graph_path)
            with open(graph_path, 'rb') as f:
                img = base64.b64encode(f.read()).decode('utf-8')

            # TODO: On my Mac, the width is double what I told Stata to export
            # Check whether this is consistent on other platforms.
            content = {
                'data': {
                    'text/plain': no_display_msg,
                    'image/png': img
                },
                'metadata': {
                    'image/png': {
                        'width': im.size[0] / 2,
                        'height': im.size[1] / 2
                    }
                }
            }
            self.send_response(self.iopub_socket, 'display_data', content)

    def do_shutdown(self, restart):
        """Shutdown the Stata session

        Shutdown the kernel. You only need to handle your own clean up - the
        kernel machinery will take care of cleaning up its own things before
        stopping.
        """
        self.stata.shutdown()
        return {'restart': restart}

    def do_is_complete(self, code):
        """Decide if command has completed

        I permit users to use /// line continuations. Otherwise, the only
        incomplete text should be unmatched braces. I use the fact that braces
        cannot be followed by text when opened or preceded or followed by text
        when closed.

        """
        if self.is_complete(code):
            return {'status': 'complete'}

        return {'status': 'incomplete', 'indent': '    '}

    def do_complete(self, code, cursor_pos):
        # Environment-aware suggestion for the current space-delimited
        # variable, local, etc.
        env, pos, chunk, rcomp = self.completions.get_env(
            code[:cursor_pos], code[cursor_pos:(cursor_pos + 2)],
            self.sc_delimit_mode)

        return {
            'status': 'ok',
            'cursor_start': pos,
            'cursor_end': cursor_pos,
            'matches': self.completions.get(chunk, env, rcomp)}

    def is_complete(self, code):
        return CodeManager(code, self.sc_delimit_mode).is_complete
