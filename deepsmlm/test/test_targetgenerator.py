import pytest
import torch

import deepsmlm.simulation.psf_kernel as psf_kernel
from deepsmlm.generic import EmitterSet, CoordinateOnlyEmitter, RandomEmitterSet, EmptyEmitterSet, test_utils as tutil
from deepsmlm.neuralfitter import target_generator


class TestTargetGenerator:

    @pytest.fixture()
    def targ(self):
        """
        Setup dummy target generator for inheritors.

        """

        class DummyTarget(target_generator.TargetGenerator):
            def __init__(self, xextent, yextent, img_shape):
                super().__init__(ix_low=0, ix_high=0)
                self.xextent = xextent
                self.yextent = yextent
                self.img_shape = img_shape

                self.delta = psf_kernel.DeltaPSF(xextent=self.xextent,
                                                 yextent=self.yextent,
                                                 img_shape=self.img_shape)

            def forward(self, em, bg=None, ix_low=None, ix_high=None):
                em, ix_low, ix_high = self._filter_forward(em, ix_low, ix_high)

                return self.delta.forward(em.xyz, em.phot, None, ix_low, ix_high).unsqueeze(1)

        xextent = (-0.5, 63.5)
        yextent = (-0.5, 63.5)
        img_shape = (64, 64)
        return DummyTarget(xextent, yextent, img_shape)

    @pytest.fixture(scope='class')
    def fem(self):
        return EmitterSet(xyz=torch.tensor([[0., 0., 0.]]), phot=torch.Tensor([1.]), frame_ix=torch.tensor([0]),
                          xy_unit='px')

    def test_shape(self, targ, fem):
        """
        Tests the frame_shape of the target output

        Args:
            targ:
            fem:

        """

        out = targ.forward(fem)

        """Tests"""
        assert out.dim() == 4, "Wrong dimensionality."
        assert out.size()[-2:] == torch.Size(targ.img_shape), "Wrong output shape."

    @pytest.mark.parametrize("ix_low,ix_high", [(0, 0), (-1, 1)])
    @pytest.mark.parametrize("em_data", [EmptyEmitterSet(xy_unit='px'), RandomEmitterSet(10, xy_unit='px')])
    def test_default_range(self, targ, ix_low, ix_high, em_data):
        targ.ix_low = ix_low
        targ.ix_high = ix_high

        """Run"""
        out = targ.forward(em_data)

        """Assertions"""
        assert out.size(0) == ix_high - ix_low + 1


class TestUnifiedEmbeddingTarget(TestTargetGenerator):

    @pytest.fixture()
    def targ(self):
        xextent = (-0.5, 63.5)
        yextent = (-0.5, 63.5)
        img_shape = (64, 64)

        return target_generator.UnifiedEmbeddingTarget(xextent, yextent, img_shape, roi_size=5, ix_low=0, ix_high=5)

    @pytest.fixture()
    def random_emitter(self):
        em = RandomEmitterSet(1000)
        em.frame_ix = torch.randint_like(em.frame_ix, low=-20, high=30)

        return em

    def test_eq_centralpx_delta(self, targ, random_emitter):
        """Check whether central pixels agree with delta function"""

        """Run"""
        mask, ix = targ._get_central_px(random_emitter.xyz, random_emitter.frame_ix)
        mask_delta = targ._delta_psf._fov_filter.clean_emitter(random_emitter.xyz)
        ix_delta = targ._delta_psf.px_search(random_emitter.xyz[mask], random_emitter.frame_ix[mask])

        """Assert"""
        assert (mask == mask_delta).all()
        for ix_el, ix_el_delta in zip(ix, ix_delta):
            assert (ix_el == ix_el_delta).all()

    @pytest.mark.parametrize("roi_size", torch.tensor([1, 3, 5, 7]))
    def test_roi_px(self, targ, random_emitter, roi_size):
        """Setup"""
        targ.__init__(xextent=targ.xextent, yextent=targ.yextent, img_shape=targ.img_shape,
                      roi_size=roi_size, ix_low=targ.ix_low, ix_high=targ.ix_high)

        """Run"""
        mask, ix = targ._get_central_px(random_emitter.xyz, random_emitter.frame_ix)
        batch_ix, x_ix, y_ix, off_x, off_y, id = targ._get_roi_px(*ix)

        """Assert"""
        assert (batch_ix.unique() == ix[0].unique()).all()
        assert (x_ix >= 0).all()
        assert (y_ix >= 0).all()
        assert (x_ix <= 63).all()
        assert (y_ix <= 63).all()
        assert batch_ix.size() == off_x.size()
        assert off_x.size() == off_y.size()

        expct_vals = torch.arange(-(targ._roi_size - 1) // 2, (targ._roi_size - 1) // 2 + 1)

        assert (off_x.unique() == expct_vals).all()
        assert (off_y.unique() == expct_vals).all()

    def test_forward(self, targ):
        """Test a couple of handcrafted cases"""

        # one emitter outside fov the other one inside
        em_set = CoordinateOnlyEmitter(torch.tensor([[-50., 0., 0.], [15.1, 19.6, 250.]]), xy_unit='px')
        em_set.phot = torch.tensor([5., 4.])

        out = targ.forward(em_set)[0]  # single frame
        assert tutil.tens_almeq(out[:, 15, 20], torch.tensor([1., 4., 0.1, -0.4, 250.]), 1e-5)
        assert tutil.tens_almeq(out[:, 16, 20], torch.tensor([0., 4., -0.9, -0.4, 250.]), 1e-5)
        assert tutil.tens_almeq(out[:, 15, 21], torch.tensor([0., 4., 0.1, -1.4, 250.]), 1e-5)


class TestJonasTarget(TestUnifiedEmbeddingTarget):

    @pytest.fixture()
    def targ(self):
        xextent = (-0.5, 63.5)
        yextent = (-0.5, 63.5)
        img_shape = (64, 64)

        return target_generator.JonasTarget(xextent, yextent, img_shape, roi_size=5, rim_max=0.6, ix_low=0, ix_high=5)


class Test4FoldTarget(TestTargetGenerator):

    @pytest.fixture()
    def targ(self):
        xextent = (-0.5, 63.5)
        yextent = (-0.5, 63.5)
        img_shape = (64, 64)

        return target_generator.FourFoldEmbedding(xextent=xextent, yextent=yextent, img_shape=img_shape,
                                                  rim_size=0.125, roi_size=3, ix_low=0, ix_high=5)

    def test_filter_rim(self, targ):

        """Setup"""
        xy = torch.tensor([[0.1, 0.9], [45.2, 47.8], [0.13, 0.9]]) - 0.5
        ix_tar = torch.tensor([0, 1, 0]).bool()

        """Run"""
        ix_out = targ._filter_rim(xy, (-0.5, -0.5), 0.125, (1., 1.))

        """Assert"""
        assert (ix_out == ix_tar).all()

    def test_forward(self, targ):

        """Setup"""
        em = EmitterSet(
            xyz=torch.tensor([[0., 0., 0.], [0.49, 0., 0.], [0., 0.49, 0.], [0.49, 0.49, 0.]]),
            phot=torch.ones(4),
            frame_ix=torch.tensor([0, 1, 2, 3]),
            xy_unit='px'
        )

        """Run"""
        tar_out = targ.forward(em, None)

        """Assert"""
        assert tar_out.size() == torch.Size([6, 20, 64, 64])
        # Negative samples
        assert tar_out[1, 0, 0, 0] == 0.
        # Positive Samples
        assert (tar_out[[0, 1, 2, 3], [0, 5, 10, 15], 0, 0] == torch.tensor([1., 1., 1., 1.])).all()

    @pytest.mark.parametrize("axis", [0, 1, 'diag'])
    def test_forward_systematic(self, targ, axis):

        """Setup"""
        pos_space = torch.linspace(-1, 1, 1001)
        xyz = torch.zeros((pos_space.size(0), 3))
        if axis == 'diag':
            xyz[:, 0] = pos_space
            xyz[:, 1] = pos_space
        else:
            xyz[:, axis] = pos_space

        em = CoordinateOnlyEmitter(xyz, xy_unit='px')
        em.frame_ix = torch.arange(pos_space.size(0)).type(em.id.dtype)

        """Run"""
        tar_outs = targ.forward(em, None, 0, em.frame_ix.max().item())

        """Assert"""
        assert (tar_outs[:, 0, 0, 0] == (pos_space >= -.375) * (pos_space < .375)).all(), "Central Pixel wrong."

        if axis == 0:
            assert (tar_outs[:, 5, 0, 0] == (pos_space >= .125) * (pos_space < .875)).all()
        elif axis == 1:
            assert (tar_outs[:, 10, 0, 0] == (pos_space >= .125) * (pos_space < .875)).all()
        elif axis == 'diag':
            assert (tar_outs[:, 15, 0, 0] == (pos_space >= .125) * (pos_space < .875)).all()
