import socket
import argparse
import simplepbr
import logging
import sys
import uuid
from modules import msgutil
from enum import Enum

import pooltool as pt
import pooltool.ani as ani
from direct.showbase.ShowBase import ShowBase
from pooltool.system.render import SystemController
from pooltool.ani.environment import Environment
from direct.gui.OnscreenText import OnscreenText
from direct.task.Task import Task

def get_initial_system(game_type: pt.GameType) -> pt.System:
    table = pt.Table.from_game_type(game_type)
    balls = pt.get_rack(game_type = game_type, table = table, ball_params=None, ballset=None, spacing_factor=1e-3)
    cue = pt.Cue(theta=0)
    system = pt.System(cue, table, balls)
    return system

class ViewerState(Enum):
    WaitingForConnection = 0
    ConnectionPending = 1
    Viewing = 2

class Viewer(ShowBase):
    def __init__(self, address, name, secret):
        super().__init__()
        simplepbr.init(enable_shadows=ani.settings["graphics"]["shadows"], max_lights=13)
        self.address = address
        self.system = get_initial_system(pt.GameType.NINEBALL)
        self.system.strike(V0 = 3, phi=pt.aim.at_ball(self.system, '1'))
        self.render.attach_new_node('scene')
        self.controller = SystemController()
        self.controller.attach_system(self.system)
        self.controller.buildup()
        self.controller.cue.hide_nodes()
        self.env = Environment()
        self.env.init(self.system.table)
        self.camLens.set_near(0.1)
        self.camLens.set_fov(53)
        self.cam.set_pos((self.system.table.w/4, self.system.table.l / 2, 2.2))
        self.cam.look_at((*self.system.table.center, 0))
        self.update_time = 0.01
        self.task_mgr.doMethodLater(self.update_time, self.update, 'update')
        self.state = ViewerState.WaitingForConnection
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.buffer: msgutil.MessageBuffer
        self.name = name
        if secret is not None:
            self.secret = uuid.UUID(hex = secret)
        else:
            self.secret = None
        self.exitFunc = self.exit
        font = self.loader.load_font('fonts/Anta/Anta-Regular.ttf')
        bg_color = (0.92, 0.83, 0.68, 0.0)
        text_color = (0.9, 0.0, 0.0, 1.0)
        self.score_display = OnscreenText(text = '', font = font, pos = (-0.88, 0.88), scale = 0.1, fg=text_color, bg = bg_color, shadow = (0.05, 0.05, 0.05, 1), shadowOffset=(0.05, 0.05))
        self.turn_indicator = OnscreenText(text = '', font = font, pos = (0.81, -0.94), scale = 0.1, fg=text_color, bg = bg_color, shadow = (0.05, 0.05, 0.05, 1), shadowOffset=(0.05, 0.05))
        self.game_over_text = OnscreenText(text = '', font = font, pos = (0, 0), scale = 0.2, fg=text_color, bg = bg_color, shadow = (0.05, 0.05, 0.05, 1), shadowOffset=(0.05, 0.05))
        self.game_over_text.hide()
        self.accept('game_over', self.on_game_over)
        self.accept('update_score', self.update_score)
        self.accept('animate_shot', self.animate_shot)

    async def on_game_over(self, winner, wait):
        await Task.pause(wait)
        self.game_over_text.text = f'GAME OVER! {winner.upper()} WON!'
        self.game_over_text.show()
        await Task.pause(2.5)
        self.game_over_text.hide()

    async def update_score(self, scores, wait):
        await Task.pause(wait)
        player_names = list(scores.keys())
        player_scores = list(scores.values())
        self.score_display.text = f'{player_names[0].upper()}: {player_scores[0]} vs. {player_names[1].upper()}: {player_scores[1]}'

    async def animate_shot(self):
        await Task.pause(1.5)
        self.controller.animate()
        self.controller.advance_to_end_of_stroke()

    def update(self, task):
        #waiting for connection
        if self.state == ViewerState.WaitingForConnection:
            try:
                self.sock.connect(self.address)
                self.sock.setblocking(False)
                self.buffer = msgutil.MessageBuffer(self.sock, run=False)
                self.buffer.push_msg(msgutil.LoginMessage(self.name, secret=self.secret, conn_type=msgutil.ConnectionType.VIEWER))
                self.state = ViewerState.ConnectionPending
            except ConnectionRefusedError:
                pass
        
        #connection pending
        elif self.state == ViewerState.ConnectionPending:
            self.buffer.update()
            msg = self.buffer.pop_msg()
            if msg is not None:
                if isinstance(msg, msgutil.ConnectionClosedMessage):
                    logging.info('Server disconnected!')
                    return task.done
                elif isinstance(msg, msgutil.LoginSuccessMessage):
                    logging.info(f'Connected! Secret: {msg.secret}')
                    self.state = ViewerState.Viewing
                elif isinstance(msg, msgutil.LoginFailedMessage):
                    logging.info(f'Failed to connect! {msg.reason}')
                    return task.done
                else:
                    logging.warning('Unexpected message!')
        #viewing
        elif self.state == ViewerState.Viewing:
            self.buffer.update()
            msg = self.buffer.pop_msg()
            if msg is not None:
                if isinstance(msg, msgutil.ConnectionClosedMessage):
                    logging.info('Server disconnected!')
                    return task.done
                elif isinstance(msg, msgutil.BroadcastMessage):
                    del self.system
                    self.turn_indicator.text = f'Active player: {msg.shot_info.player.name.upper()}'
                    self.system = msg.system
                    if msg.shot_info.game_over:
                        self.messenger.send('game_over', [msg.shot_info.winner.name, self.system.t])
                    self.messenger.send('update_score', [msg.scores, self.system.t])
                    self.controller.attach_system(self.system)
                    self.controller.buildup()
                    self.controller.build_shot_animation()
                    self.controller.cue.hide_nodes()
                    self.messenger.send('animate_shot')
                else:
                    logging.warning('Unexpected message!')
        else:
            raise NotImplementedError('Unkown state!')
        return task.again

    def exit(self):
        self.sock.close()

def main(args):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', level = args.log_level)

    viewer = Viewer((args.address, args.port), args.name, args.secret)
    viewer.run()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-level',
                        default = 'INFO',
                        choices = ['DEBUG', 'INFO', 'WARNING'],
                        type    = str,
                        dest    = 'log_level',
                        help    = 'Set logging level. Default setting is INFO.')

    parser.add_argument('-a', '--address',
                        metavar  = 'X.X.X.X',
                        type     = str,
                        dest     = 'address',
                        required = True,
                        help     = 'Set remote server address (IPv4). Required.')

    parser.add_argument('-p', '--port',
                        metavar  = 'PORT',
                        type     = int,
                        dest     = 'port',
                        required = True,
                        help     = 'Set remote server port. Required.')

    parser.add_argument('-n', '--name',
                        metavar  = 'NAME',
                        type     = str,
                        dest     = 'name',
                        required = True,
                        help     = 'Set user name. Required.')

    parser.add_argument('-s', '--secret',
                        default = None,
                        metavar = 'UUID',
                        type    = str,
                        dest    = 'secret',
                        help    = 'Login secret for authentication.')

    args = parser.parse_args()
    main(args)
